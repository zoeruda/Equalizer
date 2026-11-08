#!/usr/bin/env python3
"""Compares the CPU two builds of the app use while they show their graphs.

Usage: tests/perf.py OLD_BINARY NEW_BINARY [--quick] [--seconds N]

Each build is run in turn with a fresh temporary config, playing a music-like test
signal into a temporary silent device, so nothing reaches the speakers and your own
settings are left alone. Measured, for each build:

  playing    music, the window showing on the Equalizer page, 5 bands
  playing32  the same with 32 bands
  hidden     music, the window hidden (as when it runs in the background)
  silent     no audio, the window showing, after a minute to settle (skipped with --quick)

The window must stay on screen and uncovered while it runs: a covered or minimized
window counts as hidden, and that run is marked invalid. Quit Equalizer before
starting, as only one copy can run. Takes about 9 minutes, 5 with --quick.
"""
import argparse, array, json, math, os, platform, random, re, shutil, signal, subprocess, sys, tempfile, time, wave

RATE = 48000
HZ = os.sysconf("SC_CLK_TCK")


def run(*a):
    try:
        return subprocess.run(a, capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def music(path, seconds=30):
    """Pink noise that swells and fades, with a slowly bending tone: enough movement for the
    spectrum and level graphs to work as they do with real music. Seeded, so every run and
    every machine gets the same sound."""
    rng = random.Random(5)
    b = [0.0] * 7
    data = array.array("h")
    for i in range(RATE * seconds):
        w = rng.uniform(-1, 1)
        b[0] = 0.99886 * b[0] + w * 0.0555179
        b[1] = 0.99332 * b[1] + w * 0.0750759
        b[2] = 0.96900 * b[2] + w * 0.1538520
        b[3] = 0.86650 * b[3] + w * 0.3104856
        b[4] = 0.55000 * b[4] + w * 0.5329522
        b[5] = -0.7616 * b[5] - w * 0.0168980
        pink = (sum(b) + w * 0.5362) * 0.11
        b[6] = w * 0.115926
        t = i / RATE
        swell = 0.55 + 0.45 * math.sin(2 * math.pi * 1.7 * t)
        tone = 0.25 * math.sin(2 * math.pi * 220 * t + 3 * math.sin(2 * math.pi * 0.2 * t))
        s = max(-1.0, min(1.0, 0.35 * pink * swell + tone))
        v = int(s * 32767)
        data.append(v)
        data.append(v)
    with wave.open(path, "wb") as f:
        f.setnchannels(2), f.setsampwidth(2), f.setframerate(RATE)
        f.writeframes(data.tobytes())


def threads(pid):
    out = {}
    for tid in os.listdir(f"/proc/{pid}/task"):
        try:
            stat = open(f"/proc/{pid}/task/{tid}/stat").read()
            status = open(f"/proc/{pid}/task/{tid}/status").read()
        except OSError:
            continue
        name = "main" if tid == str(pid) else stat[stat.index("(") + 1 : stat.rindex(")")]
        fields = stat[stat.rindex(")") + 2 :].split()
        switches = sum(int(l.split()[1]) for l in status.splitlines() if "ctxt_switches" in l)
        out[tid] = (name, int(fields[11]) + int(fields[12]), switches)
    return out


def rss_mb(pid):
    for line in open(f"/proc/{pid}/status"):
        if line.startswith("VmRSS"):
            return int(line.split()[1]) // 1024
    return 0


def meters():
    return run("pw-cli", "ls", "Node").count("equalizer_meter")


def machine():
    info = {"kernel": platform.release()}
    for line in open("/proc/cpuinfo"):
        if line.startswith("model name"):
            info["cpu"] = line.split(":", 1)[1].strip()
            break
    info["cores"] = os.cpu_count()
    info["session"] = os.environ.get("XDG_SESSION_TYPE", "?")
    info["desktop"] = os.environ.get("XDG_CURRENT_DESKTOP", "?")
    gl = run("glxinfo", "-B")
    m = re.search(r"OpenGL renderer string: (.*)", gl)
    info["gl"] = m[1] if m else "unknown (glxinfo not installed)"
    info["pipewire"] = (re.findall(r"libpipewire ([\d.]+)", run("pw-cli", "--version")) or ["?"])[0]
    for pkg, rpm in [("gtk4", "gtk4"), ("libadwaita", "libadwaita"), ("mesa", "mesa-dri-drivers")]:
        q = run("pacman", "-Q", pkg)
        if q:
            info[pkg] = q.split()[-1]
        elif "not installed" not in (q := run("rpm", "-q", rpm)) and q:
            info[pkg] = q
    if os.environ.get("GSK_RENDERER"):
        info["GSK_RENDERER"] = os.environ["GSK_RENDERER"]
    return info


class Silent:
    """A temporary output device nothing can hear."""

    def __enter__(self):
        self.module = run("pactl", "load-module", "module-null-sink", "sink_name=eqperf", "sink_properties=device.description=EqPerf")
        if not self.module:
            sys.exit("Couldn't create the silent device. pactl needs pipewire-pulse (and libpulse on Arch).")
        time.sleep(0.5)
        return self

    def __exit__(self, *a):
        run("pactl", "unload-module", self.module)


def measure(binary, state, seconds, track, workdir):
    tmp = tempfile.mkdtemp(prefix="run", dir=workdir)
    env = dict(os.environ, XDG_CONFIG_HOME=os.path.join(tmp, "cfg"), GSK_DEBUG="renderer")
    # The app writes its own defaults on the way out.
    p = subprocess.Popen([binary], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.5)
    p.terminate()
    p.wait()
    path = os.path.join(tmp, "cfg", "equalizer", "state.json")
    s = json.load(open(path))
    s["output"]["device"] = "eqperf"
    s["output"]["enabled"] = True
    if state == "playing32":
        proto = s["output"]["sound"]["bands"][0]
        s["output"]["sound"]["bands"] = [
            dict(proto, freq=round(25 * (18000 / 25) ** (i / 31), 1), gain=float((i % 5) - 2) * 2, q=1.0) for i in range(32)
        ]
    json.dump(s, open(path, "w"))

    err = os.path.join(tmp, "stderr.txt")
    args = [binary] + (["--background"] if state == "hidden" else [])
    app = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=open(err, "w"))
    player = None
    try:
        time.sleep(2.0)
        if state != "silent":
            player = subprocess.Popen(
                ["sh", "-c", 'while :; do pw-play --target=equalizer_sink "$0" || exit; done', track],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        # Settle: the silent state needs the last minute of the level graph to be silence.
        time.sleep(75 if state == "silent" else 6)
        if app.poll() is not None:
            sys.exit(f"{binary} quit unexpectedly. Is another copy of Equalizer running?")
        m0 = meters()
        a, t0 = threads(app.pid), time.monotonic()
        time.sleep(seconds)
        b, dt = threads(app.pid), time.monotonic() - t0
        m1 = meters()
        rss = rss_mb(app.pid)
    finally:
        if player:
            os.killpg(player.pid, signal.SIGTERM)
            player.wait()
        app.terminate()
        app.wait()
        time.sleep(0.5)
    per = {}
    for tid, (name, ticks, sw) in b.items():
        _, ticks0, sw0 = a.get(tid, (name, 0, 0))
        cpu, wake = per.get(name, (0.0, 0.0))
        per[name] = (cpu + (ticks - ticks0) / HZ / dt * 100, wake + (sw - sw0) / dt)
    renderer = next((l.strip() for l in open(err) if "renderer" in l.lower()), "?")
    shutil.rmtree(tmp, ignore_errors=True)
    want = 0 if state == "hidden" else 2
    return {
        "total": sum(c for c, _ in per.values()),
        "main": per.get("main", (0, 0))[0],
        "audio": sum(c for n, (c, _) in per.items() if n.startswith("data-loop")),
        "wakeups": per.get("main", (0, 0))[1],
        "rss": rss,
        "valid": m0 == want and m1 == want,
        "renderer": renderer,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--quick", action="store_true", help="skip the silent state")
    ap.add_argument("--seconds", type=float, default=30, help="length of each measurement")
    o = ap.parse_args()
    builds = {"old": os.path.abspath(o.old), "new": os.path.abspath(o.new)}
    for b in builds.values():
        if not os.access(b, os.X_OK):
            sys.exit(f"Not an executable: {b}")
    if run("pgrep", "-x", "equalizer"):
        sys.exit("Equalizer is running. Quit it first (from its menu, if it runs in the background).")
    for tool in ["pactl", "pw-play", "pw-cli"]:
        if not shutil.which(tool):
            sys.exit(f"{tool} is missing.")

    states = ["playing", "playing32", "hidden"] + ([] if o.quick else ["silent"])
    workdir = tempfile.mkdtemp(prefix="eqperf")
    results = {}
    try:
        track = os.path.join(workdir, "music.wav")
        print("Making the test signal...", flush=True)
        music(track)
        info = machine()
        print("Keep the Equalizer windows on screen and uncovered until this finishes.\n", flush=True)
        with Silent():
            for state in states:
                for label, binary in builds.items():
                    r = measure(binary, state, o.seconds, track, workdir)
                    results[(state, label)] = r
                    flag = "" if r["valid"] else "   INVALID: the window wasn't showing as expected"
                    print(f"  {state:<10} {label}: total {r['total']:5.1f}%  UI {r['main']:5.1f}%  audio {r['audio']:4.1f}%  "
                          f"UI wakeups {r['wakeups']:4.0f}/s  {r['rss']} MB{flag}", flush=True)
                    info.setdefault("renderer", r["renderer"])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n=== Results to paste ===")
    for k, v in info.items():
        print(f"{k}: {v}")
    print(f"\n{'state':<10} {'UI old':>7} {'UI new':>7} {'change':>7}   {'total old':>9} {'total new':>9}   {'audio old':>9} {'audio new':>9}   {'wakeups':>11}")
    for state in states:
        a, b = results[(state, "old")], results[(state, "new")]
        change = f"{(b['main'] - a['main']) / a['main'] * 100:+.0f}%" if a["main"] >= 1 else "-"
        bad = "" if a["valid"] and b["valid"] else "  INVALID"
        print(f"{state:<10} {a['main']:6.1f}% {b['main']:6.1f}% {change:>7}   {a['total']:8.1f}% {b['total']:8.1f}%   "
              f"{a['audio']:8.1f}% {b['audio']:8.1f}%   {a['wakeups']:4.0f} → {b['wakeups']:<4.0f}{bad}")
    print("\nCPU is a percentage of one core.")


if __name__ == "__main__":
    main()
