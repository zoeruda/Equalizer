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
