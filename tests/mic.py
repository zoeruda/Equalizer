#!/usr/bin/env python3
"""End-to-end test of the Microphone tab: runs the real app and measures what its
microphone chain does to sound, then clicks through the tab like a user would.

It never opens your real microphone. It makes a fake one: a temporary virtual device
that the test plays into, and a microphone that carries what's played. The app's state
points the microphone chain at it, and the test records the app's own virtual
microphone. Every stream it starts refuses to fall back to another device, so a
missing device fails the test instead of reaching your speakers or your microphone.

The Output stays off, except in one case that turns it on with a temporary silent
device as its speakers, as tests/measure.py does. Other apps' sound goes silent for
those few seconds. The default microphone and speakers, and the app's microphone
volume, are put back afterwards. The window opens on screen and
is driven through accessibility (AT-SPI), so leave it uncovered. Quit Equalizer first,
as only one copy can run. Takes about 2 minutes.

Usage: tests/mic.py BINARY [sound] [presets] [active]
The names run only those parts. With none, it runs all three.
"""
import json, math, os, re, shutil, signal, struct, subprocess, sys, tempfile, time, wave

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else ""
APP_ID = "io.github.zoeruda.Equalizer"
RATE = 48000
LEVEL_DB = -20.0
FAKE_IN = "eqmictest_in"  # what the test plays into
FAKE_MIC = "eqmictest"  # the microphone the app reads
SOURCE = "equalizer_source"  # the app's virtual microphone
SILENT = "eqmictest_out"  # the Output's speakers, when it's on
NO_FALLBACK = "{ node.dont-fallback = true node.dont-reconnect = true }"
OUTPUT_PRESETS = ["Flat", "Bass Boost", "Treble Boost", "Smile", "Vocal Clarity", "Late Night"]
MIC_PRESETS = ["Flat", "Clear Voice", "Noisy Room", "Quiet Mic", "Laptop Mic", "Headset", "Podcast"]
EFFECTS = ["gate", "autogain", "compressor", "loudness", "limiter"]

tmp = tempfile.mkdtemp(prefix="eqmic")
cfg = os.path.join(tmp, "cfg", "equalizer")
os.makedirs(cfg)
fails = []


def run(*a):
    try:
        return subprocess.run(a, capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def sleep(s):
    time.sleep(s)


def wait_for(cond, timeout=6.0, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        sleep(step)
    return cond()


def report(ok, text):
    print(f"  {'ok  ' if ok else 'FAIL'} {text}")
    if not ok:
        fails.append(text)
    return ok


def check(name, got, want, tol=0.6):
    report(abs(got - want) <= tol, f"{name:<52} got {got:+7.2f}  want {want:+7.2f} dB")


# ---- PipeWire state ----


def graph():
    """Nodes by id, links as (output node, input node), and each client's process id."""
    try:
        objs = json.loads(run("pw-dump") or "[]")
    except json.JSONDecodeError:
        return {}, [], {}
    nodes, links, clients = {}, [], {}
    for o in objs:
        info = o.get("info") or {}
        if o.get("type") == "PipeWire:Interface:Node":
            nodes[o["id"]] = info.get("props") or {}
        elif o.get("type") == "PipeWire:Interface:Link":
            links.append((info.get("output-node-id"), info.get("input-node-id")))
        elif o.get("type") == "PipeWire:Interface:Client":
            clients[o["id"]] = str((info.get("props") or {}).get("application.process.id"))
    return nodes, links, clients


def node_exists(name):
    return any(p.get("node.name") == name for p in graph()[0].values())


def is_source(props):
    return props.get("media.class") in ("Audio/Source", "Audio/Source/Virtual")


def app_capture(pid):
    """What the app records. Returns (from microphones, other capture streams), as
    readable strings. Recording a speaker's monitor, as the Output's meters do, isn't
    using a microphone, so it's left out."""
    nodes, links, clients = graph()
    # The chain's nodes are made inside the app, so they belong to one of its clients.
    mine = {i for i, p in nodes.items() if clients.get(p.get("client.id")) == str(pid)}
    mics = sorted(
        f"{nodes[i].get('node.name')} <- {nodes[o].get('node.name')}"
        for o, i in links
        if i in mine and o in nodes and is_source(nodes[o])
    )
    streams = sorted(
        f"{p.get('node.name')} (target {p.get('target.object', '-')})"
        for i, p in nodes.items()
        if i in mine and p.get("media.class") == "Stream/Input/Audio" and str(p.get("stream.capture.sink")).lower() != "true"
    )
    return mics, streams


def app_source_outputs():
    """The app's streams that PulseAudio lists as source outputs. GNOME and KDE show
    each one as the app using the microphone, even one that records a speaker."""
    try:
        items = json.loads(run("pactl", "-f", "json", "list", "source-outputs") or "[]")
    except json.JSONDecodeError:
        return ["<pactl gave no JSON>"]
    names = [str((so.get("properties") or {}).get("node.name", "")) for so in items]
    return sorted(n for n in names if n.startswith("equalizer_"))


def default_device(key="default.configured.audio.source"):
    return run("pw-metadata", "-n", "default", "0", key)


def default_source_name():
    m = re.search(r"value:'(.*?)'", default_device())
    try:
        return json.loads(m.group(1)).get("name") if m else None
    except json.JSONDecodeError:
        return None


# ---- Sound ----


def tone(freq, db=LEVEL_DB, seconds=1.0):
    path = os.path.join(tmp, "tone.wav")
    amp = 2147483647 * 10 ** (db / 20)
    with wave.open(path, "wb") as w:
        w.setnchannels(2), w.setsampwidth(4), w.setframerate(RATE)
        data = bytearray()
        for i in range(int(RATE * seconds)):
            s = int(amp * math.sin(2 * math.pi * freq * i / RATE))
            data += struct.pack("<ii", s, s)
        w.writeframes(bytes(data))
    return path


def samples(path):
    with wave.open(path) as w:
        ch, width = w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    assert width == 4, "recorded in s32"
    return struct.unpack("<%di" % (len(raw) // 4), raw)[::ch]


def tone_db(path, freq):
    """Level of just the test frequency once it has settled, relative to full scale.
    Recorded at 32 bits, so a gate's deep cut is still measured, not lost in rounding."""
    v = samples(path)
    n = RATE // 10
    coeff = 2 * math.cos(2 * math.pi * freq / RATE)
    levels = []
    for s in range(0, max(1, len(v) - n), n):
        a = b = 0.0
        for x in v[s : s + n]:
            a, b = x + coeff * a - b, a
        levels.append(math.sqrt(max(a * a + b * b - coeff * a * b, 0.0)) * 2 / n)
    levels.sort()
    settled = levels[int(len(levels) * 0.8)] if levels else 0.0
    return 20 * math.log10(max(settled, 1e-9 * 2147483647) / 2147483647)


def peak_db(path):
    """Highest sample after the first moments of the tone."""
    v = samples(path)
    first = next((i for i, x in enumerate(v) if abs(x) > 2**16), 0)
    return 20 * math.log10(max(max(map(abs, v[first + RATE // 10 :]), default=1), 1) / 2147483647)


def measure(freq, db=LEVEL_DB, seconds=1.0):
    """Plays a tone into the fake microphone and records the app's microphone."""
    out = os.path.join(tmp, "out.wav")
    rec = subprocess.Popen(
        ["pw-record", f"--target={SOURCE}", "-P", NO_FALLBACK, "--rate", str(RATE), "--channels", "2", "--format", "s32", out],
        stderr=subprocess.DEVNULL,
    )
    sleep(0.35)
    subprocess.run(["pw-play", f"--target={FAKE_IN}", "-P", NO_FALLBACK, tone(freq, db, seconds)], stderr=subprocess.DEVNULL)
    sleep(0.15)
    rec.send_signal(signal.SIGINT)
    rec.wait()
    return tone_db(out, freq)


def response(kind, f0, gain, q, f):
    w0, w = 2 * math.pi * f0 / RATE, 2 * math.pi * f / RATE
    alpha, c, a = math.sin(w0) / (2 * q), math.cos(w0), 10 ** (gain / 40)
    co = {
        "Bell": (1 + alpha * a, -2 * c, 1 - alpha * a, 1 + alpha / a, -2 * c, 1 - alpha / a),
        "HighPass": ((1 + c) / 2, -(1 + c), (1 + c) / 2, 1 + alpha, -2 * c, 1 - alpha),
    }[kind]
    num = complex(co[0] + co[1] * math.cos(w) + co[2] * math.cos(2 * w), -co[1] * math.sin(w) - co[2] * math.sin(2 * w))
    den = complex(co[3] + co[4] * math.cos(w) + co[5] * math.cos(2 * w), -co[4] * math.sin(w) - co[5] * math.sin(2 * w))
    return 20 * math.log10(max(abs(num / den), 1e-6))


# ---- The window, through accessibility ----


def ui_app(pid):
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        a = desktop.get_child_at_index(i)
        try:
            if a and a.get_process_id() == pid:
                return a
        except GLib.Error:
            pass
    return None


def ui_all(root, role, name=None):
    """Showing objects with this role (and name), depth first. Containers can lack the
    showing state while their contents have it, so the whole tree is searched. An open
    popover can be reached from two parents, so each object is only visited once."""
    found, seen = [], set()

    def walk(o, depth):
        if depth > 60 or o in seen:
            return
        seen.add(o)
        try:
            if (
                o.get_role_name() == role
                and (name is None or o.get_name() == name)
                and o.get_state_set().contains(Atspi.StateType.SHOWING)
            ):
                found.append(o)
            for i in range(o.get_child_count()):
                c = o.get_child_at_index(i)
                if c:
                    walk(c, depth + 1)
        except GLib.Error:
            pass

    if root:
        walk(root, 0)
    return found


def ui_clickable(obj):
    action = obj.get_action_iface()
    return bool(action) and action.get_n_actions() > 0


def ui_in_list(obj):
    """Whether this sits in a list, as the entries in a preset list do. The button that
    opens the list has the same name as the preset it shows."""
    o = obj.get_parent()
    for _ in range(8):
        if o is None:
            return False
        if o.get_role_name() in ("list item", "list"):
            return True
        o = o.get_parent()
    return False


def ui_do(obj):
    return ui_clickable(obj) and obj.get_action_iface().do_action(0)


class Window:
    def __init__(self, pid):
        self.pid = pid
        self.root = wait_for(lambda: ui_app(pid), timeout=10)
        if not self.root:
            raise RuntimeError("the window isn't reachable through accessibility (AT-SPI)")

    def find(self, role, name=None, timeout=4.0):
        hits = wait_for(lambda: ui_all(self.root, role, name), timeout=timeout)
        return hits[0] if hits else None

    def click(self, role, name):
        """Clicks it and returns it, to click again later. A menu button looks different
        while its menu is open, so it can't be found again by its role then."""
        hits = wait_for(lambda: [o for o in ui_all(self.root, role, name) if ui_clickable(o)], timeout=4)
        obj = hits[0] if hits else None
        if not obj or not ui_do(obj):
            raise RuntimeError(f"couldn't click the {role} “{name}”")
        sleep(0.4)
        return obj

    def entries(self, name=None):
        return [b for b in ui_all(self.root, "button", name) if ui_clickable(b) and ui_in_list(b)]

    def tab(self, name):
        self.click("page tab", name)

    def active(self):
        sw = self.find("switch", "Process audio")
        return sw.get_state_set().contains(Atspi.StateType.CHECKED)

    def set_active(self, on):
        if self.active() != on:
            self.click("switch", "Process audio")
        if not wait_for(lambda: self.active() == on, timeout=3):
            raise RuntimeError("the Active switch didn't change")

    def preset_names(self, current):
        """Opens the showing tab's preset list, reads it, and closes it again."""
        menu = self.click("toggle button", current)
        known = set(OUTPUT_PRESETS + MIC_PRESETS)
        listed = lambda: [b.get_name() for b in self.entries() if b.get_name() in known]
        wait_for(listed)
        names = listed()
        ui_do(menu)
        sleep(0.4)
        return names

    def pick_preset(self, current, name):
        self.click("toggle button", current)
        hits = wait_for(lambda: self.entries(name))
        if not hits or not ui_do(hits[0]):
            raise RuntimeError(f"couldn't pick the preset “{name}”")
        if not self.find("toggle button", name):
            raise RuntimeError(f"the preset button doesn't show “{name}” after picking it")
        sleep(0.4)


# ---- The app ----


def flat_sound():
    """No bands and every effect off. Effect settings that aren't given take the app's
    own defaults, so the tests below only name the ones they change."""
    return {
        "bands": [],
        "preamp": 0.0,
        "balance": 0.0,
        "eq_enabled": True,
        "effects": {e: {"enabled": False, "params": {}} for e in EFFECTS},
    }


def chain(enabled, device):
    return {"enabled": enabled, "device": device, "preset": "Flat", "sound": flat_sound()}


class App:
    def __init__(self, mutate=None, mic_on=True, output_on=False, hidden=False):
        if not node_exists(FAKE_MIC):
            raise RuntimeError("the fake microphone is gone, so the app could open the real one")
        if output_on and not node_exists(SILENT):
            raise RuntimeError("the silent device is gone, so the app could play to the speakers")
        state = {
            "output": chain(output_on, SILENT if output_on else None),
            "mic": chain(mic_on, FAKE_MIC),
            "run_in_background": True,
            "autostart": False,
        }
        if mutate:
            mutate(state["mic"]["sound"])
        self.state_path = os.path.join(cfg, "state.json")
        json.dump(state, open(self.state_path, "w"))
        env = dict(os.environ, XDG_CONFIG_HOME=os.path.join(tmp, "cfg"), GDK_BACKEND="x11", GSK_RENDERER="cairo")
        # --background starts with the window hidden, as at login.
        self.p = subprocess.Popen([BIN] + (["--background"] if hidden else []), env=env, stderr=subprocess.DEVNULL)
        self.volume = None
        if wait_for(lambda: self.p.poll() is not None, timeout=1.0):
            raise RuntimeError("the app exited right away: is another copy running?")
        if mic_on:
            if not wait_for(lambda: node_exists(SOURCE), timeout=8):
                self.close()
                raise RuntimeError("the app's virtual microphone didn't appear")
            sleep(0.6)
            # WirePlumber restores whatever volume this device last had, which would
            # shift every measurement. The raw values keep any left/right difference.
            self.volume = re.findall(r": (\d+) /", run("pactl", "get-source-volume", SOURCE))
            run("pactl", "set-source-volume", SOURCE, "100%")
        else:
            sleep(1.8)

    def saved(self):
        return json.load(open(self.state_path))

    def close(self):
        if self.volume and node_exists(SOURCE):
            run("pactl", "set-source-volume", SOURCE, *self.volume)
        if self.p.poll() is None:
            self.p.terminate()
            self.p.wait()
        sleep(0.3)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def band(kind, freq, gain, q):
    return {"kind": kind, "freq": freq, "gain": gain, "q": q, "enabled": True}


def effect(name, **params):
    def mutate(sound):
        sound["effects"][name]["enabled"] = True
        sound["effects"][name]["params"].update(params)

    return mutate


# ---- The tests ----


def sound_tests():
    print("The path: with every effect off, the microphone comes out as it went in")
    with App() as app:
        mics, _ = app_capture(app.p.pid)
        report(
            any(m.startswith("equalizer_mic_in <- " + FAKE_MIC) for m in mics),
            f"the chain records the fake microphone ({', '.join(mics) or 'nothing'})",
        )
        for f in [100, 1000, 8000]:
            check(f"{f} Hz", measure(f) - LEVEL_DB, 0.0)

    print("Equalizer")
    for kind, f0, gain, q, probes in [
        ("Bell", 1000.0, 9.0, 1.4, [250, 1000, 4000]),
        ("HighPass", 1000.0, 0.0, 0.7, [250, 1000, 5000]),
    ]:
        with App(lambda s: s.update(bands=[band(kind, f0, gain, q)])):
            for f in probes:
                check(f"{kind} {f0:.0f} Hz {gain:+.0f} dB Q{q} at {f} Hz", measure(f) - LEVEL_DB, response(kind, f0, gain, q, f), tol=0.8)

    print("Noise Gate")
    with App(effect("gate", threshold=-30.0, reduction=-24.0, attack=1.0, release=50.0)):
        check("-30 threshold: a -10 dB tone passes", measure(1000, db=-10.0), -10.0, tol=1.0)
        check("-30 threshold: a -50 dB tone is lowered by 24 dB", measure(1000, db=-50.0, seconds=1.5), -74.0, tol=2.5)

    print("Auto Gain")
    with App(effect("autogain", target=-20.0, maxgain=12.0)):
        a = measure(1000, db=-40.0, seconds=5.0)
        report(-36.0 <= a <= -27.0, f"a -40 dB tone is raised, by no more than the 12 dB max boost: {a:+.1f} dB")

    print("Compressor")
    with App(effect("compressor", threshold=-30.0, ratio=4.0, knee=-1.0, attack=1.0, makeup=0.0)):
        check("-30 threshold, 4:1: a -6 dB tone comes out near -24", measure(1000, db=-6.0, seconds=1.5), -24.0, tol=2.5)
        check("-30 threshold, 4:1: a -50 dB tone is untouched", measure(1000, db=-50.0), -50.0, tol=1.0)

    print("Limiter")
    with App(effect("limiter", threshold=-12.0)):
        measure(1000, db=-3.0, seconds=1.5)
        peak = peak_db(os.path.join(tmp, "out.wav"))
        report(-16 < peak <= -11.8, f"ceiling -12: a -3 dB tone peaks at {peak:+.2f} dB")
        check("ceiling -12: a -24 dB tone is untouched", measure(1000, db=-24.0), -24.0)

    print("With the window hidden, running in the background")
    bell = band("Bell", 1000.0, 9.0, 1.4)

    def eq_and_limiter(sound):
        sound["bands"] = [bell]
        effect("limiter", threshold=-12.0)(sound)

    with App(eq_and_limiter, hidden=True):
        # -30 dB comes out of the band at -21, under the limiter's ceiling.
        check("the EQ raises a -30 dB tone by 9 dB", measure(1000, db=-30.0) + 30.0, response("Bell", 1000.0, 9.0, 1.4, 1000), tol=0.8)
        # -10 dB would come out of the band at -1, so the limiter holds it down.
        measure(1000, db=-10.0, seconds=1.5)
        peak = peak_db(os.path.join(tmp, "out.wav"))
        report(-16 < peak <= -11.8, f"the limiter holds a -10 dB tone, raised by the EQ, to {peak:+.2f} dB")
        # The meters only run while the window shows: one of them even on the Output tab
        # with the Output off. So none means the window really is hidden.
        meters = run("pw-cli", "ls", "Node").count("equalizer_meter")
        report(meters == 0, f"no level meters run while the window is hidden ({meters})")


def preset_tests():
    print("Presets: each tab lists its own")
    with App() as app:
        win = Window(app.p.pid)
        win.tab("Output")
        out = win.preset_names("Flat")
        report(out == OUTPUT_PRESETS, f"Output: {', '.join(out)}")
        win.tab("Microphone")
        mic = win.preset_names("Flat")
        report(mic == MIC_PRESETS, f"Microphone: {', '.join(mic)}")

        print("Presets: each microphone preset loads and passes sound")
        current = "Flat"
        for name in MIC_PRESETS[1:] + ["Flat"]:
            win.pick_preset(current, name)
            current = name
            saved = wait_for(lambda: app.saved()["mic"]["preset"] == name and app.saved(), timeout=3)
            report(bool(saved), f"{name}: saved as the microphone's preset")
            # Voice level in, about voice level out: the presets shape the sound, and
            # none should silence it or blow it up.
            got = measure(1000, seconds=2.0)
            report(-30.0 <= got <= -10.0, f"{name}: a -20 dB tone comes out at {got:+.1f} dB")
        report(app.saved()["output"]["preset"] == "Flat", "the Output's preset is left alone")


def active_tests():
    print("Default microphone")
    before = default_source_name()
    with App() as app:
        report(wait_for(lambda: default_source_name() == SOURCE, timeout=3), "the app is the default microphone while on")
    after = wait_for(lambda: default_source_name() != SOURCE and default_source_name(), timeout=3)
    report(after == before, f"the default is handed back on quit ({after})")

    print("Looking at the tab with Active off doesn't open a microphone")
    with App(mic_on=False) as app:
        win = Window(app.p.pid)
        win.tab("Microphone")
        sleep(1.0)
        mics, streams = app_capture(app.p.pid)
        report(not mics, f"nothing records from a microphone ({', '.join(mics) or 'none'})")
        report(not streams, f"no capture streams ({', '.join(streams) or 'none'})")

    # The steps from the bug report in Part 3 of the 0.0.6 plan, with the Output off and on.
    for output_on in [False, True]:
        active_off_steps(output_on)


def active_off_steps(output_on):
    print(f"Turning Active off on the Microphone tab, then going back to the Output (Output {'on' if output_on else 'off'})")
    with App(mic_on=False, output_on=output_on) as app:
        win = Window(app.p.pid)
        win.tab("Output")
        sleep(1.0)
        outs = app_source_outputs()
        report(not outs, f"before: the desktop sees no microphone use on the Output tab ({', '.join(outs) or 'none'})")
        win.tab("Microphone")
        win.set_active(True)
        report(bool(wait_for(lambda: node_exists(SOURCE), timeout=6)), "Active on: the virtual microphone appears")
        sleep(1.0)
        win.set_active(False)
        report(wait_for(lambda: not node_exists(SOURCE), timeout=4), "Active off: the virtual microphone is removed")
        sleep(1.0)
        for step in ["on the Microphone tab", "back on the Output tab"]:
            if step.startswith("back"):
                win.tab("Output")
                report(win.active() is output_on, f"the Active switch shows the Output as {'on' if output_on else 'off'}, as it was")
                sleep(1.5)
            mics, streams = app_capture(app.p.pid)
            report(not mics, f"{step}: nothing records from a microphone ({', '.join(mics) or 'none'})")
            report(not streams, f"{step}: no capture streams ({', '.join(streams) or 'none'})")
            outs = app_source_outputs()
            report(not outs, f"{step}: the desktop sees no microphone use ({', '.join(outs) or 'none'})")
        saved = wait_for(lambda: app.saved(), timeout=2)
        report(saved["mic"]["enabled"] is False, "the Microphone is saved as off")
        report(saved["output"]["enabled"] is output_on, f"the Output is still {'on' if output_on else 'off'}")
    if output_on:
        sink = wait_for(lambda: "equalizer_sink" not in default_device("default.configured.audio.sink"), timeout=4)
        report(sink, "the default speakers are handed back on quit")


def main():
    if len(sys.argv) < 2 or any(a not in ("sound", "presets", "active") for a in sys.argv[2:]):
        sys.exit(__doc__.strip().splitlines()[-2])
    if "true" in run("gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path", "/org/freedesktop/DBus",
                     "--method", "org.freedesktop.DBus.NameHasOwner", APP_ID):
        sys.exit("Equalizer is running. Quit it first, as only one copy can run.")
    before = {k: default_device(k) for k in ("default.configured.audio.source", "default.configured.audio.sink")}
    silent = run("pactl", "load-module", "module-null-sink", f"sink_name={SILENT}", "sink_properties=device.description=EqMicTestOutput")
    loopback = subprocess.Popen(
        [
            "pw-loopback", "-n", FAKE_MIC,
            "--capture-props", f"{{ media.class = Audio/Sink node.name = {FAKE_IN} node.description = \"Equalizer test input\" audio.position = [ FL FR ] }}",
            "--playback-props", f"{{ media.class = Audio/Source node.name = {FAKE_MIC} node.description = \"Equalizer test microphone\" audio.position = [ FL FR ] }}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_for(lambda: node_exists(FAKE_MIC) and node_exists(FAKE_IN) and node_exists(SILENT), timeout=5):
            sys.exit("couldn't create the fake microphone and the silent device")
        run("pactl", "set-source-volume", FAKE_MIC, "100%")
        run("pactl", "set-sink-volume", FAKE_IN, "100%")
        parts = {"sound": sound_tests, "presets": preset_tests, "active": active_tests}
        chosen = sys.argv[2:] or list(parts)
        for part in [parts[name] for name in chosen]:
            try:
                part()
            except RuntimeError as e:
                report(False, f"{part.__name__} stopped: {e}")
    finally:
        loopback.terminate()
        loopback.wait()
        if silent:
            run("pactl", "unload-module", silent)
        # If the app didn't hand a default back, put it back as it was.
        for key, was in before.items():
            now = default_device(key)
            if now != was and ("equalizer_" in now or "eqmictest" in now):
                m = re.search(r"value:'(.*?)'", was)
                if m:
                    run("pw-metadata", "-n", "default", "0", key, m.group(1), "Spa:String:JSON")
                else:
                    run("pw-metadata", "-n", "default", "-d", "0", key)
                print(f"  note: the test put {key} back")
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(fails)} failed" if fails else "\nAll passed")
    sys.exit(1 if fails else 0)


main()
