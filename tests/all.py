#!/usr/bin/env python3
"""Everything to run before a release, in one go, with one report at the end.

It runs, and stops at nothing: cargo fmt and clippy (clean, no warnings allowed),
shellcheck on install.sh, a check that every test compiles, `cargo test`, a release
build, tests/measure.py, tests/mic.py, tests/events.py (a short run), tests/ui.py (the
default run, and a random one), tests/memory.py --quick, and that every file that has the
version agrees. Then it prints a table, and writes ~/equalizer-release-report-<stamp>.json
with each stage's log next to it.

Usage: tests/all.py [--binary PATH] [--long] [--flatpak] [--go] [--version X.Y.Z]
                    [--only a,b] [--skip a,b] [--seed N] [--events-steps N] [--restart]

  --binary PATH     test a build made elsewhere (the release build is skipped)
  --long            also the long runs: tests/memory.py --long (hours) and a longer events.py
  --flatpak         also build the Flatpak and run tests/measure.py against it. UNTRIED: the
                    other tests find the app by its process id, which a Flatpak hides
  --go              you've already agreed to the screen and the pointer being taken, and
                    you're away: don't ask
  --version X.Y.Z   fail unless every version is this one
  --out DIR         save the report, the logs and the other tests' reports in DIR, not in ~
  --only / --skip   stage keys: fmt clippy shellcheck python unit build measure mic events ui
                    memory versions flatpak
  --restart         events.py also restarts WirePlumber and PipeWire, which interrupts every
                    sound on the session: it asks for a go-ahead of its own

The sound, window and memory stages need a display (DISPLAY is set to :0 if it isn't), take
the pointer and keyboard for tests/ui.py, and can't run beside another copy of Equalizer: it
asks you to quit yours, and never closes it itself. Cargo runs in the toolbox (--toolbox NAME,
default equalizer-dev; --no-toolbox to run it directly).

The apps under test run with MALLOC_PERTURB_ set (freed memory is overwritten, so using it
shows), on top of glibc's own checks, which are always on: a double free aborts the app, and
the tests see it as a crash. MALLOC_CHECK_=3 is added only where glibc's libc_malloc_debug.so
is installed (since glibc 2.34 it does nothing without it, and this VM doesn't have it); the
report says which it was. AddressSanitizer, for what these miss, is a one-off for Part 8.
"""
import argparse, datetime, glob, json, os, re, shlex, shutil, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
HOME = os.path.expanduser("~")
FLATPAK_ID = "io.github.zoeruda.Equalizer"

ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("--binary")
ap.add_argument("--long", action="store_true")
ap.add_argument("--flatpak", action="store_true")
ap.add_argument("--go", action="store_true")
ap.add_argument("--version")
ap.add_argument("--only", default="")
ap.add_argument("--skip", default="")
ap.add_argument("--seed", type=int, default=int(time.time()) % 100000)
ap.add_argument("--events-steps", type=int)
ap.add_argument("--restart", action="store_true")
ap.add_argument("--toolbox", default="equalizer-dev")
ap.add_argument("--no-toolbox", action="store_true")
ap.add_argument("--out")
args = ap.parse_args()

# Run by the user: one report in ~, as the other tests do, and the logs in ~/.cache. With
# --out DIR (how Claude runs it) everything, the other tests' reports too, goes in DIR.
stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
out_dir = os.path.abspath(os.path.expanduser(args.out)) if args.out else None
report_path = os.path.join(out_dir or HOME, f"equalizer-release-report-{stamp}.json")
logs = os.path.join(out_dir, f"equalizer-release-report-{stamp}-logs") if out_dir else os.path.join(HOME, ".cache", "equalizer-release-logs", stamp)
os.makedirs(logs, exist_ok=True)

ONLY = {k for k in args.only.split(",") if k}
SKIP = {k for k in args.skip.split(",") if k}
results = []  # one dict per stage: key, name, status, seconds, detail, log
# Statuses: pass, FAIL, skipped (you asked), blocked (something stopped it: counts as not done)


def wanted(key):
    return (not ONLY or key in ONLY) and key not in SKIP


def record(key, name, status, seconds=0.0, detail="", log=None):
    results.append({"key": key, "name": name, "status": status, "seconds": round(seconds, 1), "detail": detail, "log": log})
    mark = {"pass": "ok  ", "FAIL": "FAIL", "skipped": "skip", "blocked": "STOP"}[status]
    print(f"  {mark} {name}" + (f": {detail}" if detail else "") + (f"  ({seconds:.0f} s)" if seconds >= 1 else ""), flush=True)


def tail_of(path, n=12):
    try:
        lines = [l.rstrip() for l in open(path, errors="replace") if l.strip()]
    except OSError:
        return []
    return lines[-n:]


def run_cmd(key, name, argv, env=None, summary=None, timeout=None):
    """Runs a command with its output in a log, and records pass or FAIL by its exit code."""
    log = os.path.join(logs, f"{key}.log")
    print(f"... {name}", flush=True)
    start = time.monotonic()
    with open(log, "w") as f:
        f.write("$ " + " ".join(shlex.quote(a) for a in argv) + "\n")
        f.flush()
        try:
            code = subprocess.run(argv, cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            code = None
            f.write(f"\nTimed out after {timeout} s.\n")
        except OSError as e:
            code = None
            f.write(f"\n{e}\n")
    seconds = time.monotonic() - start
    lines = tail_of(log, 40)
    detail = ""
    if summary:
        hit = [l for l in lines if re.search(summary, l)]
        detail = hit[-1].strip() if hit else ""
    if code == 0:
        record(key, name, "pass", seconds, detail, log)
    else:
        record(key, name, "FAIL", seconds, detail or ("no exit code" if code is None else f"exit code {code}"), log)
        for l in tail_of(log, 12):
            print("       | " + l[:160])
    return code == 0


def cargo(*cargo_args):
    """The command that runs cargo with these arguments: in the toolbox, unless told not to."""
    if args.no_toolbox or not shutil.which("toolbox"):
        return ["cargo", *cargo_args]
    inner = ". ~/.cargo/env; cd " + shlex.quote(ROOT) + " && cargo " + " ".join(shlex.quote(a) for a in cargo_args)
    return ["toolbox", "run", "-c", args.toolbox, "bash", "-lc", inner]


def ask(prompt):
    """A line from the user, or None when nobody can answer."""
    if not sys.stdin.isatty():
        return None
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return None


# ---- Static checks ----


def stage_python():
    start = time.monotonic()
    bad = []
    for path in sorted(glob.glob(os.path.join(TESTS, "*.py"))):
        try:
            compile(open(path).read(), path, "exec")
        except SyntaxError as e:
            bad.append(f"{os.path.basename(path)}:{e.lineno} {e.msg}")
    if bad:
        record("python", "Every test compiles", "FAIL", time.monotonic() - start, "; ".join(bad))
    else:
        record("python", "Every test compiles", "pass", time.monotonic() - start)


def versions():
    """The version in each place that has one."""
    found = {}
    cargo_toml = open(os.path.join(ROOT, "Cargo.toml")).read()
    m = re.search(r'^version\s*=\s*"([^"]+)"', cargo_toml, re.M)
    found["Cargo.toml"] = m.group(1) if m else None
    lock = open(os.path.join(ROOT, "Cargo.lock")).read()
    m = re.search(r'name = "equalizer"\nversion = "([^"]+)"', lock)
    found["Cargo.lock"] = m.group(1) if m else None
    readme = open(os.path.join(ROOT, "README.md")).read()
    m = re.search(r"this is version (\d+\.\d+\.\d+)", readme)
    found["README.md"] = m.group(1) if m else None
    pkgbuild = open(os.path.join(ROOT, "packaging", "arch", "PKGBUILD")).read()
    m = re.search(r"^pkgver=(\S+)", pkgbuild, re.M)
    found["PKGBUILD"] = m.group(1) if m else None
    return found


def stage_versions():
    start = time.monotonic()
    found = versions()
    distinct = set(found.values())
    detail = ", ".join(f"{k} {v}" for k, v in found.items())
    if None in distinct or len(distinct) != 1:
        record("versions", "The versions agree", "FAIL", time.monotonic() - start, detail)
    elif args.version and distinct != {args.version}:
        record("versions", "The versions agree", "FAIL", time.monotonic() - start, f"all {distinct.pop()}, not {args.version}")
    else:
        record("versions", "The versions agree", "pass", time.monotonic() - start, f"all {distinct.pop()}")


# ---- Tests that run the app ----


def malloc_debug_library():
    for pattern in ("/usr/lib64/libc_malloc_debug.so.0", "/usr/lib/libc_malloc_debug.so.0", "/usr/lib/*-linux-gnu/libc_malloc_debug.so.0"):
        hits = glob.glob(pattern)
        if hits:
            return hits[0]
    return None


def app_env():
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    if out_dir:
        env["EQUALIZER_REPORT_DIR"] = out_dir  # where events.py and memory.py save theirs
    env["MALLOC_PERTURB_"] = "165"
    lib = malloc_debug_library()
    if lib:
        env["MALLOC_CHECK_"] = "3"
        env["LD_PRELOAD"] = lib + (":" + env["LD_PRELOAD"] if env.get("LD_PRELOAD") else "")
    return env, ("MALLOC_CHECK_=3 and MALLOC_PERTURB_" if lib else "MALLOC_PERTURB_ and glibc's default checks (no libc_malloc_debug.so here, so no MALLOC_CHECK_)")


def equalizer_running():
    return subprocess.run(["pgrep", "-x", "equalizer"], capture_output=True).returncode == 0


def py(script, *rest):
    return [sys.executable, os.path.join(TESTS, script), *rest]


def make_flatpak_wrapper(folder):
    """A command the tests can start as if it were the app: it runs the Flatpak with the
    tests' own config folder let in, as XDG_CONFIG_HOME can't reach into the sandbox."""
    path = os.path.join(folder, "equalizer-flatpak")
    with open(path, "w") as f:
        f.write(
            "#!/bin/sh\n"
            'exec flatpak run --env=XDG_CONFIG_HOME="$XDG_CONFIG_HOME" --filesystem="$XDG_CONFIG_HOME" '
            f'--env=GDK_BACKEND=x11 --env=GSK_RENDERER=cairo {FLATPAK_ID} "$@"\n'
        )
    os.chmod(path, 0o755)
    return path


def main():
    print(f"Equalizer pre-release checks, {stamp}. Logs: {logs}\n", flush=True)
    code = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, capture_output=True, text=True).stdout.strip())

    print("Static checks")
    if wanted("fmt"):
        run_cmd("fmt", "cargo fmt --check", cargo("fmt", "--check"))
    if wanted("clippy"):
        run_cmd("clippy", "cargo clippy, no warnings", cargo("clippy", "--all-targets", "--locked", "--", "-D", "warnings"))
    if wanted("shellcheck"):
        if shutil.which("shellcheck"):
            run_cmd("shellcheck", "shellcheck install.sh", ["shellcheck", os.path.join(ROOT, "install.sh")])
        else:
            record("shellcheck", "shellcheck install.sh", "blocked", detail="shellcheck isn't installed")
    if wanted("python"):
        stage_python()
    if wanted("versions"):
        stage_versions()

    print("\nBuild and unit tests")
    if wanted("unit"):
        run_cmd("unit", "cargo test --locked", cargo("test", "--locked"), summary=r"test result")
    binary = os.path.abspath(args.binary) if args.binary else os.path.join(ROOT, "target", "release", "equalizer")
    if args.binary:
        if wanted("build"):
            record("build", "Release build", "skipped", detail="--binary given")
    elif wanted("build"):
        run_cmd("build", "Release build", cargo("build", "--release", "--locked"))
    app_stages = [k for k in ("measure", "mic", "events", "ui", "memory") if wanted(k)]

    if app_stages or (args.flatpak and wanted("flatpak")):
        print("\nThe app, on silent devices")
        env, malloc_note = app_env()
        print(f"  Memory checks: {malloc_note}.")
        if not os.access(binary, os.X_OK) and app_stages:
            for k in app_stages:
                record(k, k, "blocked", detail=f"no binary at {binary}")
            app_stages = []
        if app_stages:
            if not args.go:
                said = ask(
                    "  These take the screen and, for tests/ui.py, the pointer and keyboard, for 15 to 20 minutes, and make sound\n"
                    "  on silent devices only. Step away and leave the window uncovered, then type go (or skip): "
                )
                if said != "go":
                    for k in app_stages:
                        record(k, k, "blocked", detail="not agreed to (type go, or use --go)")
                    app_stages = []
            while app_stages and equalizer_running():
                said = ask("  Equalizer is running, and only one copy can. Quit it, then press Enter (or type skip): ")
                if said is None or said == "skip":
                    for k in app_stages:
                        record(k, k, "blocked", detail="a copy of Equalizer is running")
                    app_stages = []
        steps = args.events_steps or (300 if args.long else 30)
        if "measure" in app_stages:
            run_cmd("measure", "Sound: tests/measure.py", py("measure.py", binary), env, summary=r"All passed|\d+ failed")
        if "mic" in app_stages:
            run_cmd("mic", "Microphone: tests/mic.py", py("mic.py", binary), env, summary=r"All passed|\d+ failed")
        if "events" in app_stages:
            extra = ["--restart"] if args.restart else []
            if args.restart and not args.go and ask("  events.py will restart PipeWire, which interrupts every sound on the session. Type go: ") != "go":
                extra = []
                record("events-restart", "Events with --restart", "blocked", detail="no go-ahead")
            run_cmd("events", f"Random events: tests/events.py, seed {args.seed}, {steps} steps", py("events.py", binary, "--seed", str(args.seed), "--steps", str(steps), *extra), env, summary=r"passed|failed|FAIL")
        if "ui" in app_stages:
            run_cmd("ui", "Window: tests/ui.py", py("ui.py", binary), env, summary=r"All passed|\d+ failed")
            run_cmd("ui-random", f"Window, random: tests/ui.py --random, seed {args.seed}", py("ui.py", binary, "--random", "--seed", str(args.seed), "--steps", "60"), env, summary=r"All passed|\d+ failed|passed")
        if "memory" in app_stages:
            run_cmd("memory", "PipeWire objects and memory: tests/memory.py " + ("--long" if args.long else "--quick"), py("memory.py", binary, "--long" if args.long else "--quick"), env, summary=r"passed|failed|FAIL|leak")

        if args.flatpak and wanted("flatpak"):
            print("\nFlatpak (untried: the build and the wrapper have never been run)")
            build_dir = os.path.join(HOME, ".cache", "equalizer-flatpak-build")  # not under /tmp: Flatpak's builder doesn't share it
            os.makedirs(os.path.dirname(build_dir), exist_ok=True)
            built = run_cmd(
                "flatpak-build", "Flatpak build (untried)",
                ["flatpak", "run", "org.flatpak.Builder", "--user", "--install", "--force-clean", build_dir, os.path.join(ROOT, FLATPAK_ID + ".yml")],
            )
            if built:
                wrapper_dir = os.path.join(HOME, ".cache", "equalizer-flatpak-wrapper")
                os.makedirs(wrapper_dir, exist_ok=True)
                wrapper = make_flatpak_wrapper(wrapper_dir)
                run_cmd("flatpak-measure", "Flatpak sound: tests/measure.py (untried)", py("measure.py", wrapper), env, summary=r"All passed|\d+ failed")
            else:
                record("flatpak-measure", "Flatpak sound: tests/measure.py (untried)", "blocked", detail="the build failed")

    finish(code, dirty, malloc_note=locals().get("malloc_note", ""))


def finish(commit, dirty, malloc_note):
    failed = [r for r in results if r["status"] == "FAIL"]
    blocked = [r for r in results if r["status"] == "blocked"]
    print("\n" + "=" * 78)
    print(f"{'Stage':<58}{'Result':<9}{'Seconds':>7}")
    for r in results:
        print(f"{r['name'][:57]:<58}{r['status']:<9}{r['seconds']:>7.0f}" + (f"   {r['detail'][:60]}" if r["detail"] and r["status"] != "pass" else ""))
    print("=" * 78)
    verdict = "FAILED" if failed else ("INCOMPLETE" if blocked else "ALL PASSED")
    print(f"{verdict}: {sum(r['status'] == 'pass' for r in results)} passed, {len(failed)} failed, {len(blocked)} stopped, {sum(r['status'] == 'skipped' for r in results)} skipped")
    if dirty:
        print("Note: the working tree has uncommitted changes to tracked files.")
    json.dump(
        {
            "stamp": stamp, "commit": commit, "uncommitted_changes": dirty, "seed": args.seed, "verdict": verdict,
            "options": {"long": args.long, "flatpak": args.flatpak, "binary": args.binary, "only": sorted(ONLY), "skip": sorted(SKIP)},
            "memory_checks": malloc_note, "versions": versions(), "stages": results,
        },
        open(report_path, "w"), indent=2,
    )
    print(f"Report: {report_path}")
    if not failed and not blocked and not ONLY and not SKIP:
        print("\nTo release: commit \"Release X.Y.Z\", tag vX.Y.Z, and run the release workflow.")
    sys.exit(1 if failed else (2 if blocked else 0))


try:
    main()
except KeyboardInterrupt:
    record("interrupted", "Interrupted", "blocked", detail="Ctrl-C")
    finish("", False, "")
