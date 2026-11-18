#!/usr/bin/env python3
"""Random device events against rules that must always hold.

Starts the real app on silent test devices, does random things to it from a seed
(volume changes from both sides, late and repeated device reports, devices going and
coming back, the default changing, Active switched, a player starting and stopping), and
after each one checks a short list of rules. A failure prints the seed and the steps, so
it can be run again: `--seed N`, and `--until K` runs only the first K steps.

Everything plays into silent devices, and the default speakers are a silent device too, so
nothing reaches your real ones. The window opens on screen and is driven through
accessibility (AT-SPI): leave it uncovered. Quit Equalizer first, as only one copy can run.
The default speakers are put back afterwards.

Usage: tests/events.py BINARY [--seed N] [--steps N] [--until K] [--no-sound] [--restart]
--no-sound leaves out the loudness rule (the slow one).
The Microphone tab gets a fake microphone (never your real one, which stays untouched: the
fake one is also the default microphone while the test runs, and it never goes away).
--restart restarts WirePlumber, and PipeWire with everything on it, a few times in a run. That
interrupts every sound on the session, so it needs the user's go-ahead each time.
"""
import argparse, json, os, random, re, shutil, subprocess, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *  # noqa: E402,F401,F403

DEV = "eqev_a"  # the Output's device
OTHER = "eqev_b"  # another device that comes and goes
FALL = "eqev_fall"  # the default speakers while the test runs, and where the Output goes when its device is gone
SINK = "equalizer_sink"
SOURCE = "equalizer_source"
DESCS = {"eqev_a": "EqEventsA", "eqev_b": "EqEventsB", "eqev_fall": "AAA_EqEventsFallback"}  # what the dropdown shows
FAKE_IN, FAKE_MIC = "eqev_mic_in", "eqev_mic"  # what the test plays into, and the microphone the app reads
KNOWN_LINES = [
    r"unknown resource \d+ op:7",
    r"^equalizer-trace:",
]
# Found by this test, listed at the end and in the report instead of failing every step:
# PipeWire's complaint about a message to a resource that's gone, other than the known op 7.
NOTED_LINES = [r"unknown resource \d+ op:(?!7\b)\d+"]

ap = argparse.ArgumentParser()
ap.add_argument("binary")
ap.add_argument("--seed", type=int)
ap.add_argument("--steps", type=int, default=30)
ap.add_argument("--until", type=int)
ap.add_argument("--no-sound", action="store_true")
ap.add_argument("--only", help="comma-separated event names, to aim at one thing (for example late_reports,remove_device)")
ap.add_argument("--restart", action="store_true", help="also restart WirePlumber and PipeWire now and then (interrupts the session's sound: only with the user's go-ahead)")
ap.add_argument("--delay-every", type=int, default=10, help="check the delay rule every N steps (0: never)")
args = ap.parse_args()
seed = args.seed if args.seed is not None else random.randrange(1 << 30)
rng = random.Random(seed)
BIN = os.path.abspath(args.binary)
tmp = tempfile.mkdtemp(prefix="eqevents")
cfg = os.path.join(tmp, "cfg", "equalizer")
os.makedirs(cfg)
tones = Tones(tmp)
print(f"seed {seed}")


# ---- The world ----


class World:
    def __init__(self):
        self.mods = {}
        self.player = None
        self.player_target = None
        self.active = True
        self.default_before = default_sink()
        self.source_before = run("pactl", "get-default-source") or None
        self.mic_on = False
        self.recorder = None
        self.stderr_path = os.path.join(tmp, "app.log")
        self.seen_lines = 0
        self.noted = []
        self.desktop_default = FALL
        self.wanted = DEV  # what "Plays through" is set to; None is the system default

    def setup(self):
        self.mods[FALL] = null_sink(FALL, "AAA_EqEventsFallback")
        self.mods[DEV] = null_sink(DEV, "EqEventsA")
        time.sleep(0.5)
        for n in (FALL, DEV):
            if n not in sinks():
                sys.exit(f"couldn't create the silent device {n}")
        run("pactl", "set-default-sink", FALL)
        self.start_loopback()
        run("pactl", "set-default-source", FAKE_MIC)
        state = {
            "output": {"enabled": True, "device": DEV, "preset": "Flat", "sound": flat_sound()},
            "mic": {"enabled": False, "device": FAKE_MIC, "preset": "Flat", "sound": flat_sound()},
            "run_in_background": True,
            "autostart": False,
        }
        self.state_path = os.path.join(cfg, "state.json")
        json.dump(state, open(self.state_path, "w"))
        env = dict(os.environ, XDG_CONFIG_HOME=os.path.join(tmp, "cfg"), GDK_BACKEND="x11", GSK_RENDERER="cairo", EQUALIZER_TRACE="1")
        self.log = open(self.stderr_path, "w")
        self.launch()
        if not wait_for(lambda: node_exists(SINK), timeout=10):
            self.teardown()
            sys.exit("the app's virtual device didn't appear: is another copy running?")
        time.sleep(0.8)
        self.win = Window(self.p.pid)
        run("pactl", "set-sink-volume", SINK, "100%")
        time.sleep(0.8)

    def start_loopback(self):
        # A fake microphone, so the Microphone chain never opens a real one.
        self.loopback = subprocess.Popen(
            ["pw-loopback", "-n", FAKE_MIC,
             "--capture-props", f"{{ media.class = Audio/Sink node.name = {FAKE_IN} node.description = \"EqEvents mic input\" audio.position = [ FL FR ] }}",
             "--playback-props", f"{{ media.class = Audio/Source node.name = {FAKE_MIC} node.description = \"EqEvents microphone\" audio.position = [ FL FR ] }}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_for(lambda: node_exists(FAKE_MIC) and node_exists(FAKE_IN), timeout=10):
            self.loopback.terminate()
            sys.exit("couldn't create the fake microphone")

    def launch(self):
        env = dict(os.environ, XDG_CONFIG_HOME=os.path.join(tmp, "cfg"), GDK_BACKEND="x11", GSK_RENDERER="cairo", EQUALIZER_TRACE="1")
        self.p = subprocess.Popen([BIN], env=env, stderr=self.log)

    def quit_and_restart(self, device="keep"):
        """Quits as a user does (SIGTERM, like the tray's Quit) and starts the app again,
        with the Output's "Plays through" set to `device` (a node name or None) if given.
        Returns the exit code."""
        self.p.terminate()
        try:
            code = self.p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
            code = "hung"
        time.sleep(1.0)
        if device != "keep":
            state = json.load(open(self.state_path))
            state["output"]["device"] = device
            json.dump(state, open(self.state_path, "w"))
        self.launch()
        saved_on = json.load(open(self.state_path))["output"]["enabled"]
        if saved_on:
            wait_for(lambda: node_exists(SINK), timeout=10)
        time.sleep(1.0)
        self.win = Window(self.p.pid)
        self.active = saved_on
        self.mic_on = json.load(open(self.state_path))["mic"]["enabled"]
        self.wanted = json.load(open(self.state_path))["output"]["device"]
        if self.recorder:  # a recorder doesn't survive its source going away
            self.recorder.terminate()
            self.recorder = None
        return code

    def teardown(self):
        if self.player:
            self.player.terminate()
        self.exit_code = None
        if getattr(self, "p", None):
            if self.p.poll() is None:
                self.p.terminate()
                try:
                    self.p.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.p.kill()
                    self.p.wait()
                    self.exit_code = "hung"
            if self.exit_code is None:
                self.exit_code = self.p.returncode
        time.sleep(0.3)
        for m in self.mods.values():
            unload(m)
        if self.recorder:
            self.recorder.terminate()
        if getattr(self, "loopback", None):
            self.loopback.terminate()
            self.loopback.wait()
        if self.default_before:
            run("pactl", "set-default-sink", self.default_before)
        if self.source_before:
            run("pactl", "set-default-source", self.source_before)

    def new_lines(self):
        self.log.flush()
        lines = open(self.stderr_path, errors="replace").read().splitlines()
        fresh, self.seen_lines = lines[self.seen_lines :], len(lines)
        fresh = [l for l in fresh if l.strip() and not any(re.search(k, l) for k in KNOWN_LINES)]
        self.noted += [l for l in fresh if any(re.search(k, l) for k in NOTED_LINES)]
        return [l for l in fresh if not any(re.search(k, l) for k in NOTED_LINES)]

    def have(self, name):
        return name in self.mods

    def mic_now(self):
        """Whether the Microphone's Active switch is on, from the window. Leaves the
        Output tab showing, as the rest of the test expects."""
        try:
            self.win.tab("Microphone")
            on = self.win.active()
            self.win.tab("Output")
            return on
        except Exception:
            return self.mic_on

    def active_now(self):
        """What the window says, which is the truth for the model. The switch belongs to
        the tab showing, so this looks at the Output's own."""
        try:
            self.win.tab("Output")
            return self.win.active()
        except Exception:
            return self.active


def flat_sound():
    return {
        "bands": [],
        "preamp": 0.0,
        "balance": 0.0,
        "eq_enabled": True,
        "effects": {e: {"enabled": False, "params": {}} for e in ["gate", "autogain", "compressor", "loudness", "limiter"]},
    }


w = World()

# ---- Events ----


def ev_user_volume():
    pct = rng.choice([20, 35, 50, 65, 80, 100])
    run("pactl", "set-sink-volume", SINK, f"{pct}%")
    return f"set Equalizer's device to {pct}%"


def ev_device_volume():
    pct = rng.choice([20, 35, 50, 65, 80, 100])
    run("pactl", "set-sink-volume", DEV, f"{pct}%")
    return f"set {DEV} to {pct}%"


def ev_late_reports():
    old, new = rng.sample([30, 50, 70, 90], 2)
    run("pactl", "set-sink-volume", DEV, f"{old}%")
    time.sleep(0.6)
    run("pactl", "set-sink-volume", SINK, f"{new}%")
    time.sleep(0.008)
    for _ in range(rng.choice([1, 2])):
        run("pactl", "set-sink-volume", DEV, f"{old}%")
    time.sleep(0.23)
    run("pactl", "set-sink-volume", DEV, f"{new}%")
    return f"{DEV} reports its old {old}% after a change to {new}%, then confirms"


def ev_mute():
    side = rng.choice([SINK, DEV])
    run("pactl", "set-sink-mute", side, rng.choice(["0", "1", "toggle"]))
    return f"mute changed on {side}"


def ev_split():
    side = rng.choice([SINK, DEV])
    left, right = rng.sample([30, 45, 60, 75], 2)
    run("pactl", "set-sink-volume", side, f"{left}%", f"{right}%")
    return f"set {side} to {left}%/{right}%"


def ev_burst():
    side = rng.choice([SINK, DEV])
    vals = [rng.choice([25, 40, 55, 70, 85]) for _ in range(rng.randint(3, 6))]
    for v in vals:
        run("pactl", "set-sink-volume", side, f"{v}%")
        time.sleep(rng.choice([0, 0.005, 0.02]))
    return f"{side}: {' '.join(map(str, vals))}% a few ms apart"


def ev_remove_device():
    if not w.have(DEV):
        return None
    unload(w.mods.pop(DEV))
    return f"removed {DEV}"


def ev_add_device():
    if w.have(DEV):
        return None
    w.mods[DEV] = null_sink(DEV, "EqEventsA", channels=rng.choice([2, 2, 1, 6]))
    return f"added {DEV} back"


def ev_other_device():
    if w.have(OTHER):
        unload(w.mods.pop(OTHER))
        return f"removed {OTHER}"
    w.mods[OTHER] = null_sink(OTHER, "EqEventsB", channels=rng.choice([2, 1, 6]))
    return f"added {OTHER}"


def ev_default():
    target = rng.choice([FALL, OTHER if w.have(OTHER) else FALL])
    run("pactl", "set-default-sink", target)
    w.desktop_default = target
    return f"the desktop chose {target} as the default"


def ev_active():
    on = not w.active_now()
    try:
        w.win.set_active(on)
    except RuntimeError as e:
        return f"couldn't switch Active: {e}"
    return f"Active {'on' if on else 'off'}"


def ev_tab():
    w.win.tab(rng.choice(["Output", "Microphone"]))
    return "switched tab"


def ev_player():
    if w.player:
        w.player.terminate()
        w.player = None
        return "player stopped"
    path = tones.tone(220, -45, 300, name="player.wav")
    # Like a real player: it follows the default device, which is the app's while on.
    w.player_log = os.path.join(tmp, "player.log")
    w.player = subprocess.Popen(["pw-play", path], stderr=open(w.player_log, "w"))
    return "player started on the default device"


def ev_quit():
    on = w.active_now()
    code = w.quit_and_restart()
    if code != 0:
        w.quit_failure = f"the app's exit code on quitting (Active {'on' if on else 'off'}) was {code}"
    return f"quit with Active {'on' if on else 'off'} and started again"


def ev_mic_active():
    on = not w.mic_now()
    try:
        w.win.tab("Microphone")
        w.win.set_active(on)
        w.win.tab("Output")
    except RuntimeError as e:
        return f"couldn't switch the Microphone's Active: {e}"
    w.mic_on = on
    if not on and w.recorder:  # its source is gone, and it can't fall back
        w.recorder.terminate()
        w.recorder = None
    return f"Microphone Active {'on' if on else 'off'}"


def ev_recorder():
    """Another app recording from Equalizer's microphone, starting or stopping."""
    if w.recorder:
        w.recorder.terminate()
        w.recorder = None
        return "recorder stopped"
    if not node_exists(SOURCE):
        return None
    out = os.path.join(tmp, "rec.wav")
    w.recorder = subprocess.Popen(["pw-record", f"--target={SOURCE}", "-P", NO_FALLBACK, out], stderr=subprocess.DEVNULL)
    return "a recorder started on Equalizer's microphone"


def ev_mic_sound():
    """Something said into the fake microphone."""
    subprocess.Popen(["pw-play", f"--target={FAKE_IN}", "-P", NO_FALLBACK, tones.tone(300, -40, 1.0, name="mic.wav")], stderr=subprocess.DEVNULL)
    return "a tone into the fake microphone"


def ev_plays_through():
    """Another device in the Output's "Plays through". The dropdown's list can't be picked
    through accessibility (its rows take a selection but don't activate), so this sets it in
    the saved state and starts the app again, which is how a choice is read at start. The
    live change from the dropdown is tests/ui.py's, with the pointer."""
    choices = [None] + [n for n in (DEV, OTHER, FALL) if w.have(n)]
    name = rng.choice(choices)
    on = w.active_now()
    code = w.quit_and_restart(device=name)
    if code != 0:
        w.quit_failure = f"the app's exit code on quitting (Active {'on' if on else 'off'}) was {code}"
    w.wanted = name
    return f"quit, set Plays through to {name or 'the system default'}, and started again"


def settle_after_restart():
    """Waits for PipeWire and the Pulse layer to answer again."""
    return wait_for(lambda: run("pactl", "info") != "", timeout=20, step=0.5)


def ev_restart_wireplumber():
    """WirePlumber restarted: the session manager forgets the links and the defaults it
    holds, and works them out again."""
    if w.player:  # nothing may be playing while the routing is up in the air
        w.player.terminate()
        w.player = None
    run("systemctl", "--user", "restart", "wireplumber", timeout=60)
    settle_after_restart()
    time.sleep(3.0)
    w.desktop_default = default_sink() if default_sink() in DESCS else FALL
    return "WirePlumber restarted"


def ev_restart_pipewire():
    """PipeWire, its Pulse layer and WirePlumber restarted: every device the test made, and
    the app's own, is gone and has to be made again."""
    if w.player:
        w.player.terminate()
        w.player = None
    if w.recorder:
        w.recorder.terminate()
        w.recorder = None
    w.loopback.terminate()
    w.loopback.wait()
    run("systemctl", "--user", "restart", "pipewire", "pipewire-pulse", "wireplumber", timeout=90)
    if not settle_after_restart():
        return "PipeWire did not come back"
    time.sleep(3.0)
    # The devices are the test's, and come back as they were. (A default that was the real
    # speakers' for a moment plays nothing: no player is running.)
    for name in list(w.mods):
        w.mods[name] = null_sink(name, DESCS[name])
    w.start_loopback()
    run("pactl", "set-default-sink", FALL)
    run("pactl", "set-default-source", FAKE_MIC)
    w.desktop_default = FALL
    time.sleep(3.0)
    return "PipeWire restarted, and the test's devices made again"


def ev_destroy():
    """Equalizer's device destroyed by another program. Rough, so it's rare."""
    nodes, _, _ = graph()
    ids = [i for i, p in nodes.items() if p.get("node.name") == SINK]
    if not ids:
        return None
    run("pw-cli", "destroy", str(ids[0]))
    return "Equalizer's device destroyed with pw-cli"


EVENTS = [
    (1, ev_destroy),
    (2, ev_quit),
    (3, ev_plays_through),
    (3, ev_mic_active),
    (2, ev_recorder),
    (1, ev_mic_sound),
    (6, ev_user_volume),
    (6, ev_device_volume),
    (3, ev_late_reports),
    (3, ev_mute),
    (3, ev_split),
    (3, ev_burst),
    (3, ev_remove_device),
    (4, ev_add_device),
    (2, ev_other_device),
    (2, ev_default),
    (3, ev_active),
    (2, ev_tab),
    (2, ev_player),
]


# ---- Rules ----


def expected_device():
    """Where the Output should play, from the rules of `pick_device`: the chosen device,
    otherwise the default from before the app: what the desktop last chose, if that's
    still there, else the silent fallback."""
    if w.wanted and w.have(w.wanted):
        return w.wanted
    return w.desktop_default if w.have(w.desktop_default) else FALL


def rules(step, check_sound, check_delay=False):
    problems = []

    def bad(text):
        problems.append(text)

    if getattr(w, "quit_failure", None):
        bad("1. " + w.quit_failure)
        w.quit_failure = None
    # 1. The app is running, and printed nothing unknown.
    if w.p.poll() is not None:
        bad(f"1. the app has exited with code {w.p.returncode}")
        return problems
    for l in w.new_lines():
        bad(f"1. the app printed: {l}")

    # 8. state.json is valid and says what the window shows.
    on = w.active_now()
    try:
        saved = json.load(open(w.state_path))
        if saved["mic"]["enabled"] != w.mic_now():
            bad(f"8. state.json has the Microphone {'on' if saved['mic']['enabled'] else 'off'}, the window disagrees")
        shown = plays_through(w.win)
        want_text = DESCS.get(w.wanted) if w.wanted else "System default"
        if w.wanted is None or w.have(w.wanted):
            if shown != want_text:
                bad(f"8. the Plays through dropdown shows {shown!r}, it should show {want_text!r}")
        if saved["output"]["device"] != w.wanted:
            bad(f"8. state.json has Plays through {saved['output']['device']!r}, the test chose {w.wanted!r}")
        if saved["output"]["enabled"] != on:
            bad(f"8. state.json has the Output {'on' if saved['output']['enabled'] else 'off'}, the window {'on' if on else 'off'}")
    except Exception as e:
        bad(f"8. state.json isn't valid: {e}")

    # 6. Nothing of the app's records when it shouldn't: with the Microphone off, no capture
    # streams and no source outputs; with it on, only its own stream from the fake microphone.
    mic = w.mic_now()
    w.mic_on = mic
    outs = source_outputs()
    allowed = ["equalizer_mic_in"] if mic else []
    if outs != allowed:
        bad(f"6. the desktop sees the app using a microphone: {', '.join(outs) or 'none'} (Microphone {'on' if mic else 'off'})")
    nodes, links, clients = graph()
    mine = {i for i, p in nodes.items() if clients.get(p.get("client.id")) == str(w.p.pid)}
    caps = sorted(p.get("node.name") for i, p in nodes.items() if i in mine and p.get("media.class") == "Stream/Input/Audio" and str(p.get("stream.capture.sink")).lower() != "true")
    if caps != allowed:
        bad(f"6. capture streams: {', '.join(caps) or 'none'} (Microphone {'on' if mic else 'off'})")
    if mic:
        # The chain reads the fake microphone and nothing else.
        names = sorted({nodes[o].get("node.name") for o, i in links if i in mine and nodes.get(i, {}).get("node.name") == "equalizer_mic_in" and o in nodes})
        if names != [FAKE_MIC]:
            bad(f"6. the Microphone reads {', '.join(map(str, names)) or 'nothing'}, not the fake microphone")
    if mic != node_exists(SOURCE):
        bad(f"6. the Microphone is {'on' if mic else 'off'} but Equalizer's microphone {'is not' if mic else 'is'} there")
    if w.recorder and w.recorder.poll() is not None and mic:
        bad("7. the recorder on Equalizer's microphone has stopped while the Microphone is on")

    sink_there = node_exists(SINK)
    if on and not sink_there:
        bad("4. Active is on but Equalizer's device isn't there")
    if not on and sink_there:
        bad("4. Active is off but Equalizer's device is still there")

    if on and sink_there:
        # 4. The Output plays through the device the rules give.
        want = expected_device()
        got = wait_for(lambda: linked_to("equalizer_out") == [want] and [want], timeout=4) or linked_to("equalizer_out")
        if got != [want]:
            bad(f"4. the Output plays through {'/'.join(got) or 'nothing'}, the rules give {want}")
        # 3. Equalizer's volume and mute are the device's.
        def volumes_differ():
            a, b = sink_volume(SINK), sink_volume(want)
            if not (a and b):
                return None
            (va, ma), (vb, mb) = a, b
            if ma != mb:
                return (va, ma, vb, mb)
            if len(va) == len(vb):
                ok = all(abs(x - y) <= 0.5 for x, y in zip(va, vb))
            else:  # one has fewer channels: the loudest is what they share
                ok = abs(max(va) - max(vb)) <= 0.5
            return None if ok else (va, ma, vb, mb)

        # Settled: the late reports and WirePlumber's own changes take a moment.
        diff = wait_for(lambda: not volumes_differ() or None, timeout=3.0) and None or volumes_differ()
        if diff:
            va, ma, vb, mb = diff
            bad(f"3. Equalizer shows {[round(x, 1) for x in va]} dB{' muted' if ma else ''}, {want} has {[round(x, 1) for x in vb]} dB{' muted' if mb else ''}")
        a, b = sink_volume(SINK), sink_volume(want)
        if a and b:
            (va, ma), (vb, mb) = a, b
            # 2. As loud as straight into the device.
            if check_sound and not ma and not mb and max(va) > -40:
                via = tones.measure(want, SINK, seconds=0.7)
                direct = tones.measure(want, want, seconds=0.7)
                if abs(via - direct) > 1.0:
                    bad(f"2. through the app {via:+.2f} dB, straight into the device {direct:+.2f} dB")

    # 5. No delay goes unreported, beyond 20 ms. Slow, so only now and then.
    quiet = True
    if check_delay and on and sink_there and w.have(expected_device()):
        # The clicks must be loud enough to hear after the volume (and not muted).
        v = sink_volume(expected_device())
        quiet = not v or v[1] or min(v[0]) < -15
    if check_delay and not quiet:
        want = expected_device()
        straight, through = click_delay(tmp, want, want), click_delay(tmp, SINK, want)
        if straight is None or through is None:
            seen = [l for l in run("pw-link", "-l").splitlines() if "eqevents" in l or want in l][:12]
            bad(f"5. last recording: {getattr(click_delay, 'last', '?')}")
            bad(f"5. couldn't hear the clicks (straight into {want}: {straight}, through the app: {through}; volumes {sink_volume(want)}, player {'on' if w.player else 'off'})")
        else:
            unreported = (through - straight) - (reported_ms(SINK) - reported_ms(want))
            if unreported > 20:
                bad(f"5. the app adds {through - straight:.1f} ms, {unreported:.1f} ms of it not reported (at most 20)")

    # 7. A player that was playing still is.
    if w.player:
        if w.player.poll() is not None:
            bad(f"7. the player has stopped (code {w.player.returncode}): {open(w.player_log).read().strip()[:200]}")
        else:
            name = None
            nodes, links, _ = graph()
            ids = [i for i, p in nodes.items() if p.get("node.name") == "pw-play" and p.get("media.class") == "Stream/Output/Audio"]
            if not ids or not any(o in ids for o, _ in links):
                bad("7. the player isn't linked to any device")
    return problems


# ---- Run ----

report = {"seed": seed, "binary": BIN, "steps": [], "noted": []}
failed = None
try:
    w.setup()
    n = args.until if args.until is not None else args.steps
    if args.restart:
        EVENTS += [(1, ev_restart_wireplumber), (1, ev_restart_pipewire)]
    if args.only:
        wanted = {"ev_" + n.strip() for n in args.only.split(",")}
        pool = [e for e in EVENTS if e[1].__name__ in wanted]
        if not pool:
            sys.exit("no such event: " + args.only)
    else:
        pool = EVENTS
    weights = list(pool) if args.only else [e for e in pool for _ in range(e[0])]
    for k in range(1, n + 1):
        _, ev = rng.choice(weights)
        say = ev()
        if say is None:
            say = "(nothing to do)"
        time.sleep(1.0)
        problems = rules(k, check_sound=not args.no_sound and k % 2 == 0, check_delay=bool(args.delay_every) and k % args.delay_every == 0)
        # The model follows the window after the desktop's own default change.
        w.active = w.active_now()
        print(f"{k:>3}. {say}{'' if not problems else '  <- FAIL'}")
        report["steps"].append({"n": k, "did": say, "problems": problems})
        if problems:
            for p in problems:
                print(f"       {p}")
            failed = k
            w.log.flush()
            tail = open(w.stderr_path, errors="replace").read().splitlines()[-30:]
            report["app_log_tail"] = tail
            print("   the app's last lines:")
            for l in tail:
                print("      " + l)
            report["devices"] = {n: sink_volume(n) for n in (SINK, DEV, FALL, OTHER)}
            print(f"   volumes now: {report['devices']}")
            break
finally:
    w.teardown()
    report["noted"] = w.noted
    if w.noted:
        print(f"noted, not a failure: {len(w.noted)} lines like “{w.noted[0]}”")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.expanduser(f"~/equalizer-events-report-{stamp}.json")
    try:
        json.dump(report, open(path, "w"), indent=1)
        print(f"report: {path}")
    except OSError:
        pass
    shutil.rmtree(tmp, ignore_errors=True)

# 1, at the end: quitting is clean (it crashed once after the device was destroyed).
if w.exit_code not in (0, None):
    print(f"\nFAILED: the app's exit code on quitting was {w.exit_code}")
    print(f"Run again: tests/events.py {args.binary} --seed {seed} --steps {args.steps}")
    sys.exit(1)

if failed:
    print(f"\nFAILED at step {failed}. Run again: tests/events.py {args.binary} --seed {seed} --until {failed}")
    sys.exit(1)
print("\nAll passed")
