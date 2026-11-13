#!/usr/bin/env python3
"""End-to-end audio test: runs the real app and measures what it does to sound.

Everything plays into a temporary silent device, so nothing reaches the speakers.
The app's virtual device is set to 100% while the test runs, then back to its saved
volume. It needs an X display (DISPLAY) for the window. Usage: tests/measure.py BINARY
"""
import json, math, os, re, shutil, signal, struct, subprocess, sys, tempfile, time, wave

BIN = os.path.abspath(sys.argv[1])
RATE = 48000
LEVEL_DB = -20.0
tmp = tempfile.mkdtemp(prefix="eqtest")
cfg = os.path.join(tmp, "cfg", "equalizer")
os.makedirs(cfg)
fails = []


def run(*a):
    return subprocess.run(a, capture_output=True, text=True).stdout.strip()


module = run("pactl", "load-module", "module-null-sink", "sink_name=eqmeasure", "sink_properties=device.description=EqMeasure")
assert module, "couldn't create the silent test device"
time.sleep(0.5)
assert "eqmeasure" in run("pactl", "list", "short", "sinks"), "silent test device is missing"


def tone(freq, db=LEVEL_DB, seconds=1.0):
    path = os.path.join(tmp, "tone.wav")
    amp = 32767 * 10 ** (db / 20)
    with wave.open(path, "wb") as w:
        w.setnchannels(2), w.setsampwidth(2), w.setframerate(RATE)
        data = bytearray()
        for i in range(int(RATE * seconds)):
            s = int(amp * math.sin(2 * math.pi * freq * i / RATE))
            data += struct.pack("<hh", s, s)
        w.writeframes(bytes(data))
    return path


def tone_db(path, freq):
    """Level of just the test frequency once it has settled, relative to full
    scale. Measuring only that frequency keeps the click when a tone starts from
    hiding a deep cut."""
    with wave.open(path) as w:
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    v = struct.unpack("<%dh" % (len(raw) // 2), raw)[::ch]
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
    return 20 * math.log10(max(settled, 1e-3) / 32767)


def peak_db(path):
    """Highest sample after the first moments of the tone."""
    with wave.open(path) as w:
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    v = struct.unpack("<%dh" % (len(raw) // 2), raw)[::ch]
    first = next((i for i, x in enumerate(v) if abs(x) > 8), 0)
    return 20 * math.log10(max(max(map(abs, v[first + RATE // 10 :]), default=1), 1) / 32767)


def measure(freq, db=LEVEL_DB, seconds=1.0, into="equalizer_sink"):
    """Plays a tone into the app's device (or `into`) and records what the silent device plays."""
    out = os.path.join(tmp, "out.wav")
    rec = subprocess.Popen(["pw-record", "--target=eqmeasure", "-P", "{stream.capture.sink=true}", "--rate", str(RATE), "--channels", "2", "--format", "s16", out], stderr=subprocess.DEVNULL)
    time.sleep(0.35)
    # With no fallback, a missing device fails the test instead of playing on the speakers.
    subprocess.run(["pw-play", f"--target={into}", "-P", "{ node.dont-fallback = true node.dont-reconnect = true }", tone(freq, db, seconds)], stderr=subprocess.DEVNULL)
    time.sleep(0.15)
    rec.send_signal(signal.SIGINT)
    rec.wait()
    return tone_db(out, freq)


def response(kind, f0, gain, q, f):
    w0, w = 2 * math.pi * f0 / RATE, 2 * math.pi * f / RATE
    alpha, c, a = math.sin(w0) / (2 * q), math.cos(w0), 10 ** (gain / 40)
    s = 2 * math.sqrt(a) * alpha
    co = {
        "Bell": (1 + alpha * a, -2 * c, 1 - alpha * a, 1 + alpha / a, -2 * c, 1 - alpha / a),
        "LowShelf": (a * ((a + 1) - (a - 1) * c + s), 2 * a * ((a - 1) - (a + 1) * c), a * ((a + 1) - (a - 1) * c - s), (a + 1) + (a - 1) * c + s, -2 * ((a - 1) + (a + 1) * c), (a + 1) + (a - 1) * c - s),
        "HighShelf": (a * ((a + 1) + (a - 1) * c + s), -2 * a * ((a - 1) + (a + 1) * c), a * ((a + 1) + (a - 1) * c - s), (a + 1) - (a - 1) * c + s, 2 * ((a - 1) - (a + 1) * c), (a + 1) - (a - 1) * c - s),
        "LowPass": ((1 - c) / 2, 1 - c, (1 - c) / 2, 1 + alpha, -2 * c, 1 - alpha),
        "HighPass": ((1 + c) / 2, -(1 + c), (1 + c) / 2, 1 + alpha, -2 * c, 1 - alpha),
        "Notch": (1, -2 * c, 1, 1 + alpha, -2 * c, 1 - alpha),
    }[kind]
    num = complex(co[0] + co[1] * math.cos(w) + co[2] * math.cos(2 * w), -co[1] * math.sin(w) - co[2] * math.sin(2 * w))
    den = complex(co[3] + co[4] * math.cos(w) + co[5] * math.cos(2 * w), -co[4] * math.sin(w) - co[5] * math.sin(2 * w))
    return 20 * math.log10(max(abs(num / den), 1e-6))


def default_state():
    """The app writes its own defaults on the way out, which keeps this test honest."""
    state = os.path.join(cfg, "state.json")
    env = dict(os.environ, XDG_CONFIG_HOME=os.path.join(tmp, "cfg"), GDK_BACKEND="x11", GSK_RENDERER="cairo")
    p = subprocess.Popen([BIN], env=env, stderr=subprocess.DEVNULL)
    time.sleep(2.5)
    p.terminate()
    p.wait()
    return json.load(open(state))


template = default_state()
template["output"]["device"] = "eqmeasure"
template["output"]["sound"]["effects"]["limiter"]["enabled"] = False


class App:
    def __init__(self, mutate):
        s = json.loads(json.dumps(template))
        mutate(s["output"]["sound"])
        json.dump(s, open(os.path.join(cfg, "state.json"), "w"))
        env = dict(os.environ, XDG_CONFIG_HOME=os.path.join(tmp, "cfg"), GDK_BACKEND="x11", GSK_RENDERER="cairo")
        self.p = subprocess.Popen([BIN], env=env, stderr=subprocess.DEVNULL)
        time.sleep(1.8)
        # The app's volume is the silent device's, at whatever it was last set to, which
        # would shift every measurement. The raw values keep any left/right difference.
        self.volume = re.findall(r": (\d+) /", run("pactl", "get-sink-volume", "equalizer_sink"))
        if not self.volume:
            self.p.terminate()
            self.p.wait()
            run("pactl", "unload-module", module)
            sys.exit("Couldn't read the volume of the app's virtual device")
        run("pactl", "set-sink-volume", "equalizer_sink", "100%")
        # For a moment after the app passes a volume on, the device going back to the
        # volume it had counts as a late report, not a change (STALE_WAIT).
        time.sleep(0.6)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        run("pactl", "set-sink-volume", "equalizer_sink", *self.volume)
        self.p.terminate()
        self.p.wait()
        time.sleep(0.3)


def check(name, got, want, tol=0.6):
    ok = abs(got - want) <= tol
    print(f"  {'ok  ' if ok else 'FAIL'} {name:<46} got {got:+7.2f}  want {want:+7.2f} dB")
    if not ok:
        fails.append(name)


def band(kind, freq, gain, q):
    return {"kind": kind, "freq": freq, "gain": gain, "q": q, "enabled": True}


print("The plugin's controls are all known")
# A control the app doesn't set gets a default nobody chose, and two of those were bugs:
# the equalizer's analyzer and Loudness's FFT mode. tests/known-ports.json lists every
# control the app sets or leaves on purpose (from LEFT_AT_DEFAULT in src/model.rs), so a
# control that an LSP update adds or renames fails here until someone has looked at it.


def plugin_file():
    home = os.path.expanduser("~")
    dirs = [d for d in os.environ.get("LADSPA_PATH", "").split(":") if d]
    dirs += [f"{home}/.local/lib/ladspa", f"{home}/.ladspa", "/usr/lib64/ladspa", "/usr/lib/ladspa", "/usr/local/lib/ladspa",
             "/app/lib/ladspa", "/usr/lib/x86_64-linux-gnu/ladspa"]
    return next((p for p in (os.path.join(d, "lsp-plugins-ladspa.so") for d in dirs) if os.path.exists(p)), None)


def control_inputs(path):
    """Each LSP plugin's control inputs, read from the file's LADSPA descriptors."""
    import ctypes as C

    class Descriptor(C.Structure):
        _fields_ = [("UniqueID", C.c_ulong), ("Label", C.c_char_p), ("Properties", C.c_int), ("Name", C.c_char_p),
                    ("Maker", C.c_char_p), ("Copyright", C.c_char_p), ("PortCount", C.c_ulong),
                    ("PortDescriptors", C.POINTER(C.c_int)), ("PortNames", C.POINTER(C.c_char_p))]

    get = C.CDLL(path).ladspa_descriptor
    get.restype, get.argtypes = C.POINTER(Descriptor), [C.c_ulong]
    found, i = {}, 0
    while d := get(i):
        d, i = d.contents, i + 1
        # 1 is an input and 4 a control (LADSPA_PORT_INPUT, LADSPA_PORT_CONTROL).
        found[d.Label.decode().rsplit("/", 1)[-1]] = {d.PortNames[n].decode() for n in range(d.PortCount) if d.PortDescriptors[n] & 5 == 5}
    return found


known = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "known-ports.json")))
lsp = plugin_file()
if not lsp:
    print("  FAIL couldn't find lsp-plugins-ladspa.so")
    fails.append("plugin file")
else:
    has = control_inputs(lsp)
    for plugin, ports in sorted(known.items()):
        there = has.get(plugin, set())
        new = sorted(there - set(ports))
        gone = sorted(p for p, why in ports.items() if p not in there and why == "set by the app")
        unused = sorted(p for p, why in ports.items() if p not in there and why != "set by the app")
        ok = bool(there) and not new and not gone
        print(f"  {'ok  ' if ok else 'FAIL'} {plugin}: {len(there)} controls" + ("" if there else ", so the plugin is missing"))
        for p in new:
            print(f"         “{p}” isn't known: the app neither sets it nor lists it in LEFT_AT_DEFAULT")
        for p in gone:
            print(f"         “{p}” is set by the app, and the plugin doesn't have it")
        if unused:
            print(f"         not in this plugin, which is fine: {', '.join(unused)}")
        if not ok:
            fails.append(f"{plugin}'s controls are known")

print("Filters: the graph's curve vs. measured audio")
for kind, f0, gain, q, probes in [
    ("Bell", 1000.0, 9.0, 1.4, [250, 700, 1000, 1400, 4000]),
    ("Bell", 200.0, -12.0, 3.0, [100, 200, 400]),
    ("LowShelf", 200.0, 6.0, 0.7, [50, 200, 2000]),
    ("HighShelf", 4000.0, -6.0, 0.7, [500, 4000, 16000]),
    ("LowPass", 1000.0, 0.0, 0.7, [200, 1000, 4000]),
    ("HighPass", 1000.0, 0.0, 0.7, [250, 1000, 5000]),
    ("Notch", 1000.0, 0.0, 2.0, [200, 700, 1000, 5000]),
]:
    with App(lambda s: s.update(bands=[band(kind, f0, gain, q)])):
        for f in probes:
            want = max(response(kind, f0, gain, q, f), -60)
            got = measure(f) - LEVEL_DB
            # Past -40 dB the exact depth doesn't matter, only that it's that deep.
            check(f"{kind} {f0:.0f} Hz {gain:+.0f} dB Q{q} at {f} Hz", max(got, -40) if want < -40 else got, max(want, -40), tol=0.8)

print("Several bands add up")
two = [band("Bell", 500.0, 6.0, 1.0), band("Bell", 2000.0, -6.0, 1.0)]
with App(lambda s: s.update(bands=two)):
    for f in [500, 1000, 2000]:
        want = sum(response("Bell", b["freq"], b["gain"], b["q"], f) for b in two)
        check(f"two bells at {f} Hz", measure(f) - LEVEL_DB, want)

print("Preamp and switches")
with App(lambda s: s.update(bands=[band("Bell", 1000.0, 6.0, 1.0)], preamp=-6.0)):
    check("preamp -6 plus band +6 at 1 kHz", measure(1000) - LEVEL_DB, 0.0)
with App(lambda s: s.update(bands=[band("Bell", 1000.0, 12.0, 1.0)], eq_enabled=False)):
    check("EQ switched off is flat", measure(1000) - LEVEL_DB, 0.0)
b = band("Bell", 1000.0, 12.0, 1.0)
b["enabled"] = False
with App(lambda s: s.update(bands=[b])):
    check("a muted band does nothing", measure(1000) - LEVEL_DB, 0.0)
with App(lambda s: s.update(bands=[])):
    check("no bands is flat", measure(1000) - LEVEL_DB, 0.0)


def effect(sound, name, **params):
    sound["bands"] = []
    sound["effects"][name]["enabled"] = True
    sound["effects"][name]["params"].update(params)


print("Limiter")
with App(lambda s: effect(s, "limiter", threshold=-12.0)):
    measure(1000, db=-3.0, seconds=1.5)
    peak = peak_db(os.path.join(tmp, "out.wav"))
    print(f"  {'ok  ' if -16 < peak <= -11.8 else 'FAIL'} ceiling -12: a -3 dB tone peaks at {peak:+.2f} dB")
    if not -16 < peak <= -11.8:
        fails.append("limiter ceiling")
    check("ceiling -12: a -24 dB tone is untouched", measure(1000, db=-24.0), -24.0)

print("Compressor")
with App(lambda s: effect(s, "compressor", threshold=-30.0, ratio=4.0, knee=-1.0, attack=1.0, makeup=0.0)):
    check("-30 threshold, 4:1: a -6 dB tone comes out near -24", measure(1000, db=-6.0, seconds=1.5), -24.0, tol=2.5)
    check("-30 threshold, 4:1: a -50 dB tone is untouched", measure(1000, db=-50.0), -50.0, tol=1.0)

print("Loudness")
with App(lambda s: effect(s, "loudness", amount=30.0)):
    mid = measure(1000)
    low = measure(60)
    check("the 1 kHz midrange stays about the same", mid - LEVEL_DB, 0.0, tol=3.0)
    print(f"  bass lift at 60 Hz relative to 1 kHz: {low - mid:+.1f} dB")
    if low - mid < 6:
        fails.append("loudness bass lift")
        print("  FAIL loudness should lift bass by a clear amount")

print("Auto gain")
with App(lambda s: effect(s, "autogain", target=-20.0, maxgain=24.0)):
    a = measure(1000, db=-40.0, seconds=4.0)
    print(f"  a -40 dB tone comes out at {a:+.1f} dB")
    if a < -36:
        fails.append("autogain boost")
        print("  FAIL auto gain should raise a quiet tone")

print("Volume comes after the effects")
# 50% on the cubic volume curve is -18 dB. Applied before the chain, Auto Gain and the
# compressor would take it for quieter sound and win most of it back.
for name in ["autogain", "compressor"]:
    with App(lambda s: effect(s, name)):
        full = measure(1000, db=-14.0, seconds=3.0)
        run("pactl", "set-sink-volume", "equalizer_sink", "50%")
        half = measure(1000, db=-14.0, seconds=3.0)
        check(f"{name} on: 50% volume lowers the output by 18 dB", half - full, 60 * math.log10(0.5))

print("Active on is as loud as Active off")
# The app's volume is the real device's, so the same sound plays as loud through the
# app as straight into the device, at any volume.
with App(lambda s: None):
    run("pactl", "set-sink-volume", "eqmeasure", "40%")
    time.sleep(0.6)
    shown = re.findall(r"(\d+)%", run("pactl", "get-sink-volume", "equalizer_sink"))
    print(f"  {'ok  ' if shown == ['40', '40'] else 'FAIL'} the device set to 40%: the app's volume shows {'/'.join(shown)}%")
    if shown != ["40", "40"]:
        fails.append("app shows the device's volume")
    check("the device at 40%: through the app vs straight into it", measure(1000) - measure(1000, into="eqmeasure"), 0.0, tol=0.3)
    run("pactl", "set-sink-volume", "equalizer_sink", "70%")
    time.sleep(0.6)
    device = re.findall(r"(\d+)%", run("pactl", "get-sink-volume", "eqmeasure"))
    print(f"  {'ok  ' if device == ['70', '70'] else 'FAIL'} the app set to 70%: the device follows to {'/'.join(device)}%")
    if device != ["70", "70"]:
        fails.append("device follows the app's volume")
    check("at 70%: through the app vs straight into the device", measure(1000) - measure(1000, into="eqmeasure"), 0.0, tol=0.3)

print("Sound stays in time with the picture")
# A player keeps the picture in step with the sound by the delay PipeWire reports. A
# delay the chain adds without reporting it makes the picture early: about 45 ms is
# noticeable. The Loudness Compensator's FFT mode used to add 160 ms, even when off.


def click_delay(into):
    """Milliseconds from a click leaving a player to it coming out of the silent device.
    The player is linked both to `into` and to a recorder that also takes the silent
    device's monitor, so both land in the same recording whatever the start-up timing."""
    every = RATE // 2
    clicks = os.path.join(tmp, "clicks.wav")
    with wave.open(clicks, "wb") as w:
        w.setnchannels(2), w.setsampwidth(2), w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<hh", s, s) for s in (16000 if i % every < 48 else 0 for i in range(RATE * 3))))

    def ports(prefix, flag):
        for _ in range(50):
            found = sorted(p for p in run("pw-link", flag).splitlines() if p.startswith(prefix))
            if len(found) >= 2:
                return found
            time.sleep(0.1)
        return []

    out = os.path.join(tmp, "clicks-out.wav")
    rec = subprocess.Popen(["pw-record", "--target=0", "-P", "{ node.name = eqmeasure_rec }", "--rate", str(RATE), "--channels", "4",
                            "--channel-map", "AUX0,AUX1,AUX2,AUX3", "--format", "s16", out], stderr=subprocess.DEVNULL)
    rec_in = ports("eqmeasure_rec:", "-i")
    for s, d in zip(ports("eqmeasure:monitor_", "-o"), rec_in[2:]):
        run("pw-link", s, d)
    # Unlinked until linked here, so it can't play anywhere else.
    play = subprocess.Popen(["pw-play", "--target=0", "-P", "{ node.name = eqmeasure_play node.dont-fallback = true }", clicks], stderr=subprocess.DEVNULL)
    play_out = ports("eqmeasure_play:", "-o")
    for s, d in zip(play_out, rec_in[:2]):
        run("pw-link", s, d)
    for s, d in zip(play_out, ports(f"{into}:playback_", "-i")):
        run("pw-link", s, d)
    play.wait()
    time.sleep(0.4)
    rec.send_signal(signal.SIGINT)
    rec.wait()
    with wave.open(out) as w:
        raw = w.readframes(w.getnframes())
    v = struct.unpack("<%dh" % (len(raw) // 2), raw)

    def onsets(x):
        found, i = [], 0
        while i < len(x):
            if abs(x[i]) > 1500:
                found.append(i)
                i += every // 2
            else:
                i += 1
        return found

    # Each click coming out is timed by its loudest sample, not its first loud one: a
    # linear-phase filter rings before the click itself. The first click may come before
    # every link is made.
    came = [abs(x) for x in v[2::4]]
    gaps = []
    for g in onsets(v[0::4])[1:]:
        window = came[g : g + every]
        if window and max(window) > 1500:
            gaps.append(window.index(max(window)))
    gaps.sort()
    return gaps[len(gaps) // 2] / RATE * 1000 if gaps else None


def reported_ms(sink):
    """The latency PipeWire reports for a device, as a Pulse client such as a browser sees it."""
    for block in run("pactl", "list", "sinks").split("\n\n"):
        if re.search(rf"Name: {re.escape(sink)}$", block, re.M):
            m = re.search(r"Latency: (\d+) usec", block)
            return int(m.group(1)) / 1000 if m else 0.0
    return 0.0


def with_effects(*names):
    def mutate(s):
        for name in names:
            s["effects"][name]["enabled"] = True
    return mutate


# Loudness off matters most: the plugin keeps its delay when bypassed.
for label, mutate in [("the defaults (limiter on)", with_effects("limiter")), ("loudness and limiter on", with_effects("loudness", "limiter"))]:
    with App(mutate):
        straight, through = click_delay("eqmeasure"), click_delay("equalizer_sink")
        if straight is None or through is None:
            print(f"  FAIL {label}: couldn't hear the clicks")
            fails.append(f"sound stays in time, {label}")
            continue
        added = through - straight
        unreported = added - (reported_ms("equalizer_sink") - reported_ms("eqmeasure"))
        ok = unreported <= 20
        print(f"  {'ok  ' if ok else 'FAIL'} {label}: the app adds {added:.1f} ms, {unreported:.1f} ms of it not reported (at most 20)")
        if not ok:
            fails.append(f"sound stays in time, {label}")

print("Each effect on its own, at neutral settings")
# An effect that's on and set to do nothing must change neither the level nor the delay.
# A plugin control the app leaves at its default can break either one, as Loudness's FFT
# mode did. The tone is at Auto Gain's target, so that has nothing to correct either.


def neutral(name):
    def mutate(s):
        s["bands"] = []
        if name == "eq":
            s["bands"] = [band("Bell", 1000.0, 0.0, 1.0), band("LowShelf", 100.0, 0.0, 0.7), band("HighShelf", 8000.0, 0.0, 0.7)]
            return
        s["effects"][name]["enabled"] = True
        s["effects"][name]["params"].update({
            "autogain": {"target": LEVEL_DB},
            "compressor": {"ratio": 1.0, "makeup": 0.0},
            "loudness": {"amount": 0.0},
            "limiter": {},
        }[name])
    return mutate


with App(lambda s: s.update(bands=[])):
    off = {f: measure(f, seconds=3.0) for f in (60, 1000, 8000)}
    off_delay = click_delay("equalizer_sink")
for name in ["autogain", "compressor", "eq", "loudness", "limiter"]:
    with App(neutral(name)):
        for f in (60, 1000, 8000):
            # Auto Gain measures loudness as the ear hears it, so only 1 kHz is at its target.
            if name == "autogain" and f != 1000:
                continue
            check(f"{name} on, doing nothing, at {f} Hz", measure(f, seconds=3.0) - off[f], 0.0, tol=0.5)
        delay = click_delay("equalizer_sink")
        if delay is None or off_delay is None:
            print(f"  FAIL {name}: couldn't hear the clicks")
            fails.append(f"{name} on its own adds no delay")
            continue
        ok = abs(delay - off_delay) <= 5
        print(f"  {'ok  ' if ok else 'FAIL'} {name} on adds {delay - off_delay:+.1f} ms (at most 5)")
        if not ok:
            fails.append(f"{name} on its own adds no delay")

print("Bluetooth headphones' late volume reports")
# After a volume change, Bluetooth headphones report the volume they had once more, a
# few milliseconds later, then confirm the new one. The app used to take the old one
# for a change on the device and put it back on its own volume.
with App(lambda s: None):
    run("pactl", "set-sink-volume", "eqmeasure", "50%")
    time.sleep(0.6)
    run("pactl", "set-sink-volume", "equalizer_sink", "70%")
    time.sleep(0.008)
    run("pactl", "set-sink-volume", "eqmeasure", "50%")
    run("pactl", "set-sink-volume", "eqmeasure", "50%")
    time.sleep(0.23)
    run("pactl", "set-sink-volume", "eqmeasure", "70%")
    time.sleep(1.0)
    shown = re.findall(r"(\d+)%", run("pactl", "get-sink-volume", "equalizer_sink"))
    device = re.findall(r"(\d+)%", run("pactl", "get-sink-volume", "eqmeasure"))
    ok = shown == device == ["70", "70"]
    print(f"  {'ok  ' if ok else 'FAIL'} set to 70%: the app shows {'/'.join(shown)}%, the device is at {'/'.join(device)}%")
    if not ok:
        fails.append("late volume reports")
    check("after the reports: through the app vs straight into the device", measure(1000) - measure(1000, into="eqmeasure"), 0.0, tol=0.3)

print("The Output comes back to its device")
# Bluetooth headphones replace their output when they switch to their headset profile and
# back, and a USB device can be unplugged and plugged in again. The app moves to another
# device while it's gone, and has to come back. Nothing plays here.


def out_linked_to():
    objs = json.loads(run("pw-dump") or "[]")
    names = {o["id"]: (o.get("info") or {}).get("props", {}).get("node.name") for o in objs if o["type"].endswith("Node")}
    out = [i for i, n in names.items() if n == "equalizer_out"]
    return sorted({names.get(o["info"]["input-node-id"]) for o in objs
                   if o["type"].endswith("Link") and out and o["info"]["output-node-id"] == out[0]})


def returning_device():
    return run("pactl", "load-module", "module-null-sink", "sink_name=eqreturn",
               "sink_properties=device.description=EqReturn")


ret = returning_device()
# Listed first, so that the app moves there while the device is gone, not to a real one.
fallback = run("pactl", "load-module", "module-null-sink", "sink_name=eqfallback",
               "sink_properties=device.description=\"AAA EqFallback\"")
time.sleep(0.5)
device_before = template["output"]["device"]
template["output"]["device"] = "eqreturn"
with App(lambda s: None):
    first = out_linked_to()
    run("pactl", "unload-module", ret)
    time.sleep(1.0)
    away = out_linked_to()
    ret = returning_device()
    time.sleep(1.0)
    back = out_linked_to()
    ok = first == ["eqreturn"] and away == ["eqfallback"] and back == ["eqreturn"]
    print(f"  {'ok  ' if ok else 'FAIL'} plays through {'/'.join(first) or '-'}, then {'/'.join(away) or '-'} while it's gone, "
          f"then {'/'.join(back) or '-'} when it's back")
    if not ok:
        fails.append("comes back to its device")
template["output"]["device"] = device_before
run("pactl", "unload-module", ret)
run("pactl", "unload-module", fallback)

print("Equalizer's device is removed by another program")
# PipeWire's filter-chain then removes itself. The app used to remove it a second time
# when quitting or turning off, which crashed it.
with App(lambda s: None) as app:
    objs = json.loads(run("pw-dump") or "[]")
    sink = [o["id"] for o in objs
            if o["type"].endswith("Node") and (o.get("info") or {}).get("props", {}).get("node.name") == "equalizer_sink"]
    run("pw-cli", "destroy", str(sink[0])) if sink else None
    time.sleep(1.0)
    gone = sink and "equalizer_sink" not in run("pactl", "list", "short", "sinks")
    alive = app.p.poll() is None
    app.p.terminate()
    try:
        code = app.p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        app.p.kill()
        code = "none (it hung)"
    ok = gone and alive and code == 0
    print(f"  {'ok  ' if ok else 'FAIL'} the device {'went' if gone else 'stayed'}, the app {'kept running' if alive else 'stopped'}, "
          f"and its exit code on quitting was {code}")
    if not ok:
        fails.append("survives its device being removed")
after = run("pw-metadata", "-n", "default", "0", "default.configured.audio.sink")
if "equalizer_sink" in after:
    fails.append("restores default after its device is removed")
    print("  FAIL the default wasn't handed back")
else:
    print("  ok   the default is handed back")

print("Default device")
before = run("pw-metadata", "-n", "default", "0", "default.configured.audio.sink")
with App(lambda s: None):
    during = run("pw-metadata", "-n", "default", "0", "default.configured.audio.sink")
    if "equalizer_sink" not in during:
        fails.append("takes over default")
        print("  FAIL the app should be the default device while on")
    else:
        print("  ok   the app is the default device while on")
after = run("pw-metadata", "-n", "default", "0", "default.configured.audio.sink")
if "equalizer_sink" in after:
    fails.append("restores default")
    print("  FAIL the default wasn't handed back on quit")
else:
    print("  ok   the default is handed back on quit")

run("pactl", "unload-module", module)
shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{len(fails)} failed" if fails else "\nAll passed")
sys.exit(1 if fails else 0)
