#!/usr/bin/env python3
"""Looks for memory leaks: runs the real app through what a user does again and again,
and checks that its memory and its PipeWire objects don't grow.

Usage: tests/memory.py BINARY [SCENARIO ...] [--long] [--minutes N] [--eq-only] [--quick] [--limit MB] [--wrap COMMAND]

Scenarios, all of them when none is named:

  playback  music playing, the window on the Equalizer page, then on an effect page:
            a 3-minute warm-up, then 2 minutes each, 30 minutes each with --long, or
            N minutes each with --minutes N. --eq-only leaves out the effect page
  hide      hiding and showing the window, 100 times
  pages     200 switches between the Equalizer and the effect pages, then 200 between
            the Output and Microphone tabs
  active    turning Active off and on, 50 times on each tab
  presets   loading every built-in preset on both tabs, then adding bands up to 32 and
            removing them all, 5 times each
  devices   adding and removing a device, 30 times

Each scenario starts with a warm-up that isn't counted, so caches and the first
allocations don't count as growth. Playback's is 3 minutes: on Arch with an Intel Arc,
the app's memory jumps 13-15 MB once in the first 2 minutes of playing, with either
renderer, then stays flat, and a line fitted over that jump would look like a leak. After it, the app's memory is sampled and a line is
fitted to it. A scenario fails when that line grows more than --limit MB (5 by default)
per 100 cycles, or per hour of playback. It's judged on the app's private memory
(Private_Clean + Private_Dirty, from /proc/PID/smaps_rollup): the memory only it holds,
which other apps starting or quitting can't move, as they move PSS. PSS and RSS are
shown next to it. It also fails when the app's PipeWire objects (its nodes, their ports,
and their links) aren't back to what they were before it: a meter stream left behind is
both a leak and the kind of bug that leaves the microphone in use.

Every reading (the time or cycle, private memory, PSS, RSS and swap) is saved to
~/equalizer-memory-report-DATE-TIME.json, with each scenario's result, so the shape of
the growth can be seen and not only its slope. Playback's warm-up readings are saved
too, marked "warm_up", with negative hours. It's saved even when the run stops. With
GSK_DEBUG=renderer, it also holds the renderer GTK used.

It uses a fresh temporary config and never reaches your speakers or your microphone:
the Output plays through a temporary silent device, and the Microphone records a fake
microphone that the test plays into. Every stream the test starts refuses to fall back
to another device. While it runs the app is the default speakers and microphone, so
other apps' sound goes silent. The defaults are put back afterwards.

The window opens on screen and is driven through accessibility (AT-SPI). Keep it on
screen and uncovered: a covered window stops its meters, and the run is invalid.
Quit Equalizer first, as only one copy can run. Takes about 30 minutes, or about 90
with --long. --quick runs a tenth of each count, to try the test itself after changing
it: its runs are too short to tell whether memory really grows, so it shows the numbers
as not judged, and only checks the PipeWire objects.

When a scenario fails, run it alone under a heap profiler to find the source, for
example: tests/memory.py BINARY hide --wrap heaptrack
"""
import argparse, array, datetime, json, math, os, random, re, shlex, shutil, signal, subprocess, sys, tempfile, time, wave

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

APP_ID = "io.github.zoeruda.Equalizer"
RATE = 48000
SILENT = "eqmem_out"  # the Output's speakers
EXTRA = "eqmem_extra"  # the device the devices scenario adds and removes
FAKE_IN = "eqmem_in"  # what the test plays into, to reach the fake microphone
FAKE_MIC = "eqmem_mic"  # the microphone the app records
SINK = "equalizer_sink"  # the app's virtual speakers
SOURCE = "equalizer_source"  # the app's virtual microphone
NO_FALLBACK = "{ node.dont-fallback = true node.dont-reconnect = true }"
OUTPUT_PRESETS = ["Flat", "Bass Boost", "Treble Boost", "Smile", "Vocal Clarity", "Late Night"]
MIC_PRESETS = ["Flat", "Clear Voice", "Noisy Room", "Quiet Mic", "Laptop Mic", "Headset", "Podcast"]
OUTPUT_EFFECTS = ["autogain", "compressor", "loudness", "limiter"]
MIC_EFFECTS = ["gate", "autogain", "compressor", "limiter"]
# The sidebar's rows, in the order of each chain. The Equalizer is the third on both.
EQ_ROW = 2

tmp = tempfile.mkdtemp(prefix="eqmem")
cfg = os.path.join(tmp, "cfg", "equalizer")
os.makedirs(cfg)
fails = []
results = []
recorded = []  # each scenario's readings, for the saved report
STARTED = time.monotonic()


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
    print(f"  {'ok  ' if ok else 'FAIL'} {text}", flush=True)
    if not ok:
        fails.append(text)
    return ok


# ---- Sound ----


def music(path, seconds=30):
    """Pink noise that swells and fades, with a slowly bending tone, as in tests/perf.py:
    enough movement for the graphs to work as they do with real music."""
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
        v = int(max(-1.0, min(1.0, 0.35 * pink * swell + tone)) * 32767)
        data.append(v)
        data.append(v)
    with wave.open(path, "wb") as f:
        f.setnchannels(2), f.setsampwidth(2), f.setframerate(RATE)
        f.writeframes(data.tobytes())


class Player:
    """Plays the track over and over into one device. It never falls back to another:
    while the app's virtual speakers are gone, it waits for them to come back."""

    def __init__(self, target, track):
        self.p = subprocess.Popen(
            ["sh", "-c", 'while :; do pw-play --target="$1" -P "$2" "$0"; sleep 0.3; done', track, target, NO_FALLBACK],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def stop(self):
        os.killpg(self.p.pid, signal.SIGTERM)
        self.p.wait()


# ---- PipeWire state ----


def dump():
    try:
        return json.loads(run("pw-dump") or "[]")
    except json.JSONDecodeError:
        return []


def node_names():
    return {
        (o.get("info") or {}).get("props", {}).get("node.name")
        for o in dump()
        if o.get("type") == "PipeWire:Interface:Node"
    }


def node_exists(name):
    return name in node_names()


def meters():
    return sum(1 for n in node_names() if n and n.startswith("equalizer_meter"))


def objects(pid):
    """The app's nodes (by name), how many ports they have, and their links to the test's
    own devices and to each other. Links to other apps' streams are left out, as those
    come and go with whatever else is playing."""
    objs = dump()
    clients = {
        o["id"]
        for o in objs
        if o.get("type") == "PipeWire:Interface:Client"
        and str(((o.get("info") or {}).get("props") or {}).get("application.process.id")) == str(pid)
    }
    names, mine = {}, {}
    for o in objs:
        if o.get("type") == "PipeWire:Interface:Node":
            props = (o.get("info") or {}).get("props") or {}
            names[o["id"]] = str(props.get("node.name"))
            if props.get("client.id") in clients:
                mine[o["id"]] = names[o["id"]]
    ports = sum(
        1
        for o in objs
        if o.get("type") == "PipeWire:Interface:Port" and ((o.get("info") or {}).get("props") or {}).get("node.id") in mine
    )
    ours = lambda i: i in mine or names.get(i, "").startswith(("eqmem", "equalizer_"))
    links = []
    for o in objs:
        if o.get("type") == "PipeWire:Interface:Link":
            info = o.get("info") or {}
            a, b = info.get("output-node-id"), info.get("input-node-id")
            if (a in mine or b in mine) and ours(a) and ours(b):
                links.append(f"{names.get(a)} → {names.get(b)}")
    return {"nodes": sorted(mine.values()), "ports": ports, "links": sorted(links)}


def describe(objs):
    return f"{len(objs['nodes'])} nodes, {objs['ports']} ports, {len(objs['links'])} links"


def difference(before, after):
    out = []
    for key in ("nodes", "links"):
        b, a = list(before[key]), list(after[key])
        for x in list(b):
            if x in a:
                a.remove(x)
                b.remove(x)
        out += [f"+{x}" for x in a] + [f"-{x}" for x in b]
    if before["ports"] != after["ports"]:
        out.append(f"ports {before['ports']} → {after['ports']}")
    return ", ".join(out)


def default_device(key):
    return run("pw-metadata", "-n", "default", "0", key)


# ---- Memory ----


def memory(pid):
    """Private memory, PSS, RSS and swap, in MB. Private memory is the pages only the app
    maps. PSS adds a share of the pages it shares, so it moves when other apps map or
    unmap the same libraries; RSS counts them whole."""
    vals = {}
    for line in open(f"/proc/{pid}/smaps_rollup"):
        key, _, rest = line.partition(":")
        if key in ("Pss", "Rss", "Private_Clean", "Private_Dirty", "Swap"):
            vals[key] = int(rest.split()[0]) / 1024
    return {
        "private": vals["Private_Clean"] + vals["Private_Dirty"],
        "pss": vals["Pss"],
        "rss": vals["Rss"],
        "swap": vals.get("Swap", 0.0),
    }


def fit(points):
    """Least-squares slope of (x, y) points."""
    n = len(points)
    if n < 2:
        return 0.0
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    den = sum((x - mx) ** 2 for x, _ in points)
    return sum((x - mx) * (y - my) for x, y in points) / den if den else 0.0


class Samples:
    """Memory after the warm-up, against a count of cycles or hours. Its readings go into
    the saved report as they're taken, so a run that stops keeps them."""

    def __init__(self, app, name, unit, per):
        self.app, self.name, self.unit, self.per = app, name, unit, per
        self.readings = []
        self.record = {"scenario": name, "x": "hours" if unit == "hour" else "cycles", "readings": self.readings}
        recorded.append(self.record)

    def take(self, x, warm_up=False):
        m = memory(self.app.pid)
        reading = {"x": round(x, 6), "t": round(time.monotonic() - STARTED, 1), **{k: round(v, 3) for k, v in m.items()}}
        if warm_up:
            reading["warm_up"] = True
        self.readings.append(reading)

    def judge(self, limit):
        judged = [r for r in self.readings if not r.get("warm_up")]
        points = lambda key: [(r["x"], r[key]) for r in judged]
        growth = {key: fit(points(key)) * self.per for key in ("private", "pss", "rss")}
        first, last = judged[0], judged[-1]
        span = last["x"] - first["x"]
        span = f"{span * 60:.0f} minutes" if self.unit == "hour" else f"{span:g} {self.unit}"
        text = (
            f"{self.name}: private {first['private']:.1f} → {last['private']:.1f} MB over {span}, "
            f"{growth['private']:+.2f} MB per {self.per:g} {self.unit} (limit {limit:g}). "
            f"PSS {first['pss']:.1f} → {last['pss']:.1f}, RSS {first['rss']:.0f} → {last['rss']:.0f} MB"
        )
        # --quick's runs are too short to tell a leak from the app settling.
        if QUICK:
            print(f"  --   {text}, not judged (--quick)", flush=True)
        else:
            report(growth["private"] <= limit, text)
        verdict = "not judged" if QUICK else "ok" if growth["private"] <= limit else "FAIL"
        per = f"MB per {self.per:g} {self.unit}"
        self.record.update(growth={k: round(v, 3) for k, v in growth.items()}, growth_unit=per, limit=limit, verdict=verdict)
        results.append((self.name, first["private"], last["private"], growth["private"], per, verdict))


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


def ui_all(root, match):
    """Showing objects that match, depth first. Containers can lack the showing state
    while their contents have it, so the whole tree is searched. An open popover can be
    reached from two parents, so each object is only visited once."""
    found, seen = [], set()

    def walk(o, depth):
        if depth > 60 or o in seen:
            return
        seen.add(o)
        try:
            if match(o) and o.get_state_set().contains(Atspi.StateType.SHOWING):
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


def named(role, name=None):
    return lambda o: o.get_role_name() == role and (name is None or o.get_name() == name)


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


def ui_do(obj, action=0):
    return ui_clickable(obj) and obj.get_action_iface().do_action(action)


class Window:
    def __init__(self, pid):
        self.root = wait_for(lambda: ui_app(pid), timeout=10)
        if not self.root:
            raise RuntimeError("the window isn't reachable through accessibility (AT-SPI)")
        self.buttons = {}

    def find(self, match, timeout=4.0):
        hits = wait_for(lambda: ui_all(self.root, match), timeout=timeout)
        return hits[0] if hits else None

    def click(self, match, what, pause=0.4):
        """Clicks it and returns it, to click again later. A menu button looks different
        while its menu is open, so it can't be found again by its role then."""
        hits = wait_for(lambda: [o for o in ui_all(self.root, match) if ui_clickable(o)], timeout=4)
        if not hits or not ui_do(hits[0]):
            raise RuntimeError(f"couldn't click {what}")
        sleep(pause)
        return hits[0]

    def button(self, name):
        """A button that stays where it is, found once and clicked quickly."""
        if name not in self.buttons:
            self.buttons[name] = self.find(named("button", name))
            if not self.buttons[name]:
                raise RuntimeError(f"couldn't find the button “{name}”")
        return self.buttons[name]

    def tab(self, name):
        tab = self.click(named("page tab", name), f"the {name} tab", pause=0)
        if not wait_for(lambda: tab.get_state_set().contains(Atspi.StateType.SELECTED), timeout=3):
            raise RuntimeError(f"the {name} tab didn't open")
        sleep(0.15)

    def page(self, row):
        """Shows a page by selecting its row in the showing tab's sidebar. Right after a
        tab switch, the sidebar may not be showing yet, so it's tried for a moment."""
        def select():
            lists = [l for l in ui_all(self.root, named("list")) if l.get_child_count() == 5 and l.get_selection_iface()]
            if len(lists) != 1:
                return False
            sel = lists[0].get_selection_iface()
            try:
                return sel.is_child_selected(row) or (sel.select_child(row) and sel.is_child_selected(row))
            except GLib.Error:
                return False

        if not wait_for(select, timeout=4):
            raise RuntimeError(f"couldn't select row {row} in the sidebar")

    def active(self):
        sw = self.find(named("switch", "Process audio"))
        return sw.get_state_set().contains(Atspi.StateType.CHECKED)

    def set_active(self, on):
        if self.active() != on:
            self.click(named("switch", "Process audio"), "the Active switch", pause=0.1)
        if not wait_for(lambda: self.active() == on, timeout=3):
            raise RuntimeError("the Active switch didn't change")

    @staticmethod
    def preset_menu():
        """The button that opens the preset list. It's named after the preset it shows."""
        return lambda o: o.get_role_name() == "toggle button" and o.get_description() == "Presets"

    def pick_preset(self, name):
        """Opens the showing tab's preset list and picks one. It's tried up to three
        times, as a list that's still opening can miss the click."""
        shows = lambda: self.find(lambda o: self.preset_menu()(o) and o.get_name() == name, timeout=3)
        for _ in range(3):
            # Right after a tab switch, the other tab's preset button can still be showing.
            menus = wait_for(lambda: (h := [o for o in ui_all(self.root, self.preset_menu()) if ui_clickable(o)]) and len(h) == 1 and h)
            if not menus or not ui_do(menus[0]):
                raise RuntimeError("couldn't open the preset menu")
            hits = wait_for(lambda: [b for b in ui_all(self.root, named("button", name)) if ui_clickable(b) and ui_in_list(b)], timeout=3)
            if hits and ui_do(hits[0]) and shows():
                sleep(0.2)
                return
            # Close the list if it's still open, then try again.
            if any(ui_in_list(b) for b in ui_all(self.root, named("button")) if ui_clickable(b)):
                ui_do(menus[0])
            sleep(0.5)
        raise RuntimeError(f"couldn't pick the preset “{name}”")

    def frame(self):
        """The window, a child of the app. The EQ graph is a frame too, further down.
        It's looked up each time, as the one found before the window first showed
        goes away."""
        kids = lambda: [self.root.get_child_at_index(i) for i in range(self.root.get_child_count())]
        return wait_for(lambda: next((c for c in kids() if c and c.get_role_name() == "frame"), None), timeout=5)

    def hide(self):
        """Closes the window as its close button does. With a chain on, that hides it."""
        frame = self.frame()
        action = frame and frame.get_action_iface()
        names = [action.get_action_name(i) for i in range(action.get_n_actions())] if action else []
        try:
            ok = "window.close" in names and action.do_action(names.index("window.close"))
        except GLib.Error as e:
            raise RuntimeError(f"couldn't close the window: {e.message}")
        if not ok:
            raise RuntimeError("couldn't close the window")


def show():
    """Opens the app again, as its launcher does. The running copy shows its window."""
    run("gdbus", "call", "--session", "--dest", APP_ID, "--object-path", "/" + APP_ID.replace(".", "/"),
        "--method", "org.freedesktop.Application.Activate", "{}")


# ---- The app ----


def bands(n):
    return [
        {"kind": "Bell", "freq": round(40 * (16000 / 40) ** (i / max(1, n - 1)), 1), "gain": float((i % 5) - 2) * 2, "q": 1.0, "enabled": True}
        for i in range(n)
    ]


def sound(effects, on):
    return {
        "bands": bands(5),
        "preamp": 0.0,
        "balance": 0.0,
        "eq_enabled": True,
        "effects": {e: {"enabled": e in on, "params": {}} for e in effects},
    }


def dbus_pid():
    out = run("gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path", "/org/freedesktop/DBus",
              "--method", "org.freedesktop.DBus.GetConnectionUnixProcessID", APP_ID)
    m = re.search(r"uint32 (\d+)", out)
    return int(m.group(1)) if m else None


class App:
    """The app, with both chains on, playing music into each."""

    def __init__(self, binary, wrap, track):
        for name in (SILENT, FAKE_MIC, FAKE_IN):
            if not node_exists(name):
                raise RuntimeError(f"{name} is gone, so the app could reach a real device")
        # The bands aren't any preset's, so they're named as the app names an edited sound.
        state = {
            "output": {"enabled": True, "device": SILENT, "preset": "Custom", "sound": sound(OUTPUT_EFFECTS, ["compressor", "limiter"])},
            "mic": {"enabled": True, "device": FAKE_MIC, "preset": "Custom", "sound": sound(MIC_EFFECTS, ["gate", "compressor"])},
            "run_in_background": True,
            "autostart": False,
        }
        self.state_path = os.path.join(cfg, "state.json")
        json.dump(state, open(self.state_path, "w"))
        self.err = os.path.join(tmp, "stderr.txt")
        env = dict(os.environ, XDG_CONFIG_HOME=os.path.join(tmp, "cfg"))
        self.p = subprocess.Popen(shlex.split(wrap) + [binary], env=env, stdout=subprocess.DEVNULL, stderr=open(self.err, "w"))
        self.players = []
        if wait_for(lambda: self.p.poll() is not None, timeout=1.0):
            raise RuntimeError("the app exited right away: is another copy running?")
        # With --wrap, the app may be a child of the process started here.
        self.pid = wait_for(dbus_pid, timeout=30 if wrap else 10)
        if not self.pid or not wait_for(lambda: node_exists(SINK) and node_exists(SOURCE), timeout=10):
            self.close()
            raise RuntimeError("the app's virtual devices didn't appear")
        self.players = [Player(SINK, track), Player(FAKE_IN, track)]
        self.win = Window(self.pid)
        if not wait_for(lambda: meters() > 0, timeout=5):
            self.close()
            raise RuntimeError("the window shows no meters: is it covered?")

    def alive(self):
        if self.p.poll() is not None:
            raise RuntimeError("the app quit")

    def saved(self):
        try:
            return json.load(open(self.state_path))
        except (OSError, json.JSONDecodeError):
            return None

    def objects(self):
        return objects(self.pid)

    def back_to(self, before, what):
        """Whether the app's PipeWire objects come back to what they were."""
        after = wait_for(lambda: (o := self.objects()) == before and o, timeout=8) or self.objects()
        report(after == before, f"{what}: PipeWire objects as before ({describe(after)}){'' if after == before else ': ' + difference(before, after)}")

    def close(self):
        for p in self.players:
            p.stop()
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
        sleep(0.3)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ---- The scenarios ----


QUICK = False


def scaled(n):
    """A tenth of the count with --quick, for trying the test itself."""
    return max(3, n // 10) if QUICK else n


def cycles(app, name, count, step, limit, sample_every=1, warm=3):
    """Runs step(i) for a warm-up, then count times, sampling memory as it goes. The
    warm-up is at least a tenth of the count, and at least `warm` steps."""
    count = scaled(count)
    sample_every = 1 if QUICK else sample_every
    warm = max(warm, count // 10)
    for i in range(warm):
        step(i)
    app.alive()
    s = Samples(app, name, "cycles", 100)
    s.take(0)
    for i in range(1, count + 1):
        step(warm + i)
        if i % sample_every == 0 or i == count:
            app.alive()
            s.take(i)
    s.judge(limit)


def settle(app):
    """Gives the meters and graphs time to fill after a change, so the objects compared
    are the steady ones."""
    sleep(1.5)
    app.alive()


def playback(app, o):
    warm_up = 10 if QUICK else 180
    minutes = o.minutes if o.minutes else 30 if o.long else 0.5 if QUICK else 2
    length = f"{warm_up / 60:g}-minute" if warm_up >= 60 else f"{warm_up}-second"
    print(f"Playback: a {length} warm-up, then {minutes:g} minutes on each page, the window showing", flush=True)
    win = app.win
    pages = [("the Equalizer page", "Output", EQ_ROW), ("the Compressor page", "Output", 1)]
    for title, tab, row in pages[:1] if o.eq_only else pages:
        win.tab(tab)
        win.page(row)
        s = Samples(app, f"playback, {title}", "hour", 1)
        # The warm-up, sampled but not judged. Its readings get negative hours. On Arch
        # with an Intel Arc, memory jumps 13-15 MB once in the first 2 minutes, with
        # Vulkan or GL, and with 0.0.5's graph too, then stays flat: the renderer's and
        # the driver's caches filling, not a leak.
        t0 = time.monotonic() + warm_up
        while time.monotonic() < t0:
            app.alive()
            s.take((time.monotonic() - t0) / 3600, warm_up=True)
            sleep(min(5, max(0, t0 - time.monotonic())))
        before = app.objects()
        end = t0 + minutes * 60
        while time.monotonic() < end:
            app.alive()
            s.take((time.monotonic() - t0) / 3600)
            if meters() == 0:
                raise RuntimeError("the meters stopped: is the window covered or minimized?")
            sleep(5)
        s.take((time.monotonic() - t0) / 3600)
        s.judge(o.limit)
        app.back_to(before, f"playback, {title}")


def hide(app, o):
    print("Hiding and showing the window", flush=True)
    win = app.win
    win.tab("Output")
    win.page(EQ_ROW)
    settle(app)
    before = app.objects()

    def step(i):
        win.hide()
        if not wait_for(lambda: meters() == 0, timeout=5):
            raise RuntimeError("the meters didn't stop with the window hidden")
        sleep(0.3)
        show()
        if not wait_for(lambda: meters() > 0, timeout=5):
            raise RuntimeError("the meters didn't come back with the window showing: is it covered?")
        sleep(0.7)

    cycles(app, "hide and show", 100, step, o.limit)
    settle(app)
    app.back_to(before, "hide and show")


def pages(app, o):
    print("Switching pages and tabs", flush=True)
    win = app.win
    win.tab("Output")
    win.page(EQ_ROW)
    settle(app)
    before = app.objects()
    # Each step shows a different page, so every one is shown in turn, the Equalizer
    # every other time.
    order = [EQ_ROW, 0, EQ_ROW, 1, EQ_ROW, 3, EQ_ROW, 4]

    def page_step(i):
        win.page(order[i % len(order)])
        sleep(0.25)

    # The warm-up shows every page once, as the first showing of each lays it out.
    cycles(app, "switching pages", 200, page_step, o.limit, sample_every=10, warm=2 * len(order))

    win.tab("Microphone")  # the Microphone's pages, once each, before the warm-up
    for row in [0, 1, 3, 4, EQ_ROW]:
        win.page(row)
        sleep(0.25)

    def tab_step(i):
        win.tab("Microphone" if i % 2 == 0 else "Output")
        sleep(0.25)

    win.tab("Output")
    win.page(EQ_ROW)
    cycles(app, "switching tabs", 200, tab_step, o.limit, sample_every=10)
    win.tab("Output")
    win.page(EQ_ROW)
    settle(app)
    app.back_to(before, "switching pages and tabs")


def active(app, o):
    print("Turning Active off and on", flush=True)
    win = app.win
    for tab, device in [("Output", SINK), ("Microphone", SOURCE)]:
        win.tab(tab)
        win.page(EQ_ROW)
        settle(app)
        before = app.objects()

        # With the Output off, the app hands the default speakers back and waits for the
        # streams that follow the default to leave its virtual speakers. The test's
        # player never moves, so it can't reach your speakers: the app waits until its
        # time limit and removes them with the player attached. That says so in its log.
        def step(i):
            win.set_active(False)
            if not wait_for(lambda: not node_exists(device), timeout=6):
                raise RuntimeError(f"{device} stayed with Active off")
            sleep(0.3)
            win.set_active(True)
            if not wait_for(lambda: node_exists(device), timeout=6):
                raise RuntimeError(f"{device} didn't come back with Active on")
            sleep(0.7)

        cycles(app, f"Active off and on, {tab}", 50, step, o.limit)
        settle(app)
        app.back_to(before, f"Active off and on, {tab}")
    saved = wait_for(lambda: (s := app.saved()) and s["output"]["enabled"] and s["mic"]["enabled"] and s, timeout=4)
    report(bool(saved), "both chains are saved as on afterwards")


def presets(app, o):
    print("Loading presets, adding and removing bands", flush=True)
    win = app.win
    win.tab("Output")
    win.page(EQ_ROW)
    settle(app)
    before = app.objects()
    lists = [("Output", OUTPUT_PRESETS[1:] + OUTPUT_PRESETS[:1]), ("Microphone", MIC_PRESETS[1:] + MIC_PRESETS[:1])]
    rounds = 2 if QUICK else 5

    def preset_step(i):
        for tab, names in lists:
            win.tab(tab)
            for name in names:
                win.pick_preset(name)

    # One cycle is one preset loaded, so a round is 13 of them.
    per_round = sum(len(names) for _, names in lists)
    preset_step(0)
    app.alive()
    s = Samples(app, "loading presets", "cycles", 100)
    s.take(0)
    for r in range(1, rounds + 1):
        preset_step(r)
        app.alive()
        s.take(r * per_round)
    s.judge(o.limit)

    win.tab("Output")
    win.page(EQ_ROW)
    win.pick_preset("Flat")
    add, remove = win.button("Add a band (or double-click the graph)"), win.button("Remove this band")
    # The Equalizer's tile in the sidebar shows the count, as "Flat · 5 bands".
    tile = win.find(lambda o: o.get_role_name() == "label" and re.fullmatch(r"(?!LSP ).* · \d+ bands", o.get_name() or ""))
    if not tile:
        raise RuntimeError("couldn't find the band count in the sidebar")
    shown = lambda: int(re.search(r"(\d+) bands", tile.get_name()).group(1))

    def press(button, want):
        """Clicks and waits for the count to change. Clicks sent too quickly get lost."""
        for _ in range(3):
            ui_do(button)
            if wait_for(lambda: shown() == want, timeout=1.5, step=0.02):
                return
        raise RuntimeError(f"the band count stayed at {shown()}, instead of {want}")

    def band_step(i):
        for n in range(shown() + 1, 33):
            press(add, n)
        for n in range(shown() - 1, -1, -1):
            press(remove, n)
        saved = wait_for(lambda: (s := app.saved()) and len(s["output"]["sound"]["bands"]) == 0 and s, timeout=5)
        if not saved:
            raise RuntimeError("the bands weren't saved as removed")

    # One cycle is one band added and removed, so a round is 32 of them.
    band_step(0)
    app.alive()
    s = Samples(app, "adding and removing bands", "cycles", 100)
    s.take(0)
    for r in range(1, rounds + 1):
        band_step(r)
        app.alive()
        s.take(r * 32)
    s.judge(o.limit)
    settle(app)
    app.back_to(before, "presets and bands")


def devices(app, o):
    print("Adding and removing a device", flush=True)
    win = app.win
    win.tab("Output")
    win.page(EQ_ROW)
    settle(app)
    before = app.objects()

    def step(i):
        module = run("pactl", "load-module", "module-null-sink", f"sink_name={EXTRA}", "sink_properties=device.description=EqMemExtra")
        if not module or not wait_for(lambda: node_exists(EXTRA), timeout=5):
            raise RuntimeError("couldn't add the device")
        sleep(0.7)
        run("pactl", "unload-module", module)
        if not wait_for(lambda: not node_exists(EXTRA), timeout=5):
            raise RuntimeError("couldn't remove the device")
        sleep(0.7)

    cycles(app, "adding and removing a device", 30, step, o.limit)
    settle(app)
    app.back_to(before, "adding and removing a device")


SCENARIOS = {"playback": playback, "hide": hide, "pages": pages, "active": active, "presets": presets, "devices": devices}


def save_report(path, stamp, binary, chosen, o, renderers):
    """Every reading, with what the run was, so runs on different machines compare."""
    report = {
        "started": stamp,
        "finished": datetime.datetime.now().strftime("%Y%m%d-%H%M%S"),
        "binary": binary,
        "scenarios": chosen,
        "options": {"long": o.long, "minutes": o.minutes, "eq_only": o.eq_only, "quick": o.quick, "limit": o.limit, "wrap": o.wrap},
        "environment": {k: os.environ[k] for k in ("GSK_RENDERER", "GSK_DEBUG", "GDK_BACKEND", "GDK_SCALE", "XDG_SESSION_TYPE") if k in os.environ},
        "renderer_used": renderers or "unknown (set GSK_DEBUG=renderer to record it)",
        "units": "MB; x is cycles, or hours of playback; t is seconds since the test started",
        "results": recorded,
        "failed": fails,
    }
    try:
        with open(path, "w") as f:
            json.dump(report, f, indent=1)
    except OSError as e:
        print(f"\nCouldn't save the readings to {path}: {e}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], usage=__doc__.split("\n\n")[1].split(": ", 1)[1])
    ap.add_argument("binary")
    ap.add_argument("scenarios", nargs="*", metavar="SCENARIO")
    ap.add_argument("--long", action="store_true", help="play for 30 minutes on each page after the warm-up, instead of 2")
    ap.add_argument("--minutes", type=float, help="play for this many minutes on each page after the warm-up (overrides --long and --quick)")
    ap.add_argument("--eq-only", action="store_true", help="play on the Equalizer page only, not then on the Compressor page")
    ap.add_argument("--limit", type=float, default=5.0, help="growth of private memory allowed, in MB per 100 cycles or per hour (default 5)")
    ap.add_argument("--quick", action="store_true", help="a tenth of each count, to try the test itself: too short to judge a leak, so it doesn't")
    ap.add_argument("--wrap", default="", help="a command to run the app under, such as heaptrack")
    o = ap.parse_args()
    if o.minutes is not None and o.minutes <= 0:
        ap.error("--minutes must be more than 0")
    global QUICK
    QUICK = o.quick
    for name in o.scenarios:
        if name not in SCENARIOS:
            ap.error(f"no scenario {name!r}. They are: {', '.join(SCENARIOS)}")
    binary = os.path.abspath(o.binary)
    if not os.access(binary, os.X_OK):
        sys.exit(f"Not an executable: {binary}")
    if dbus_pid():
        sys.exit("Equalizer is running. Quit it first, as only one copy can run.")
    for tool in ["pactl", "pw-play", "pw-dump", "pw-loopback", "pw-metadata", "gdbus"]:
        if not shutil.which(tool):
            sys.exit(f"{tool} is missing.")
    chosen = o.scenarios or list(SCENARIOS)

    keys = ("default.configured.audio.source", "default.configured.audio.sink")
    before = {k: default_device(k) for k in keys}
    silent = run("pactl", "load-module", "module-null-sink", f"sink_name={SILENT}", "sink_properties=device.description=EqMemOutput")
    loopback = subprocess.Popen(
        [
            "pw-loopback", "-n", FAKE_MIC,
            "--capture-props", f"{{ media.class = Audio/Sink node.name = {FAKE_IN} node.description = \"Equalizer memory test input\" audio.position = [ FL FR ] }}",
            "--playback-props", f"{{ media.class = Audio/Source node.name = {FAKE_MIC} node.description = \"Equalizer memory test microphone\" audio.position = [ FL FR ] }}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    app = None
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    saved_to = os.path.expanduser(f"~/equalizer-memory-report-{stamp}.json")
    try:
        if not wait_for(lambda: node_exists(FAKE_MIC) and node_exists(FAKE_IN) and node_exists(SILENT), timeout=5):
            sys.exit("couldn't create the fake microphone and the silent device")
        track = os.path.join(tmp, "music.wav")
        print("Making the test signal...", flush=True)
        music(track)
        print("Keep the Equalizer window on screen and uncovered until this finishes.\n", flush=True)
        app = App(binary, o.wrap, track)
        objs = app.objects()
        m = memory(app.pid)
        print(f"Started: {describe(objs)}, private {m['private']:.1f} MB, PSS {m['pss']:.1f} MB\n", flush=True)
        for name in chosen:
            try:
                SCENARIOS[name](app, o)
            except RuntimeError as e:
                report(False, f"{name} stopped: {e}")
                app.alive()
    except RuntimeError as e:
        report(False, f"stopped: {e}")
    except KeyboardInterrupt:
        # The readings so far are still saved.
        report(False, "stopped: interrupted")
    finally:
        renderers = []
        if app:
            app.close()
            messages = open(app.err, errors="replace").read()
            # GTK says which renderer it used for each surface with GSK_DEBUG=renderer.
            renderers = sorted(set(re.findall(r"Using renderer '?(\w+)", messages)))
            if fails and messages:
                print("\nThe app's messages:\n" + messages[-3000:])
        loopback.terminate()
        loopback.wait()
        if silent:
            run("pactl", "unload-module", silent)
        # If the app didn't hand a default back, put it back as it was.
        for key, was in before.items():
            now = default_device(key)
            if now != was and ("equalizer_" in now or "eqmem" in now):
                m = re.search(r"value:'(.*?)'", was)
                if m:
                    run("pw-metadata", "-n", "default", "0", key, m.group(1), "Spa:String:JSON")
                else:
                    run("pw-metadata", "-n", "default", "-d", "0", key)
                print(f"  note: the test put {key} back")
        shutil.rmtree(tmp, ignore_errors=True)
        if recorded:
            save_report(saved_to, stamp, binary, chosen, o, renderers)

    if results:
        print("\n=== Results to paste ===")
        print(f"{'scenario':<40} {'private start':>13} {'private end':>11}  growth")
        for name, first, last, growth, per, verdict in results:
            print(f"{name:<40} {first:12.1f}M {last:10.1f}M  {growth:+.2f} {per}{', not judged' if verdict == 'not judged' else ''}")
    if recorded:
        print(f"\nReadings saved to {saved_to}")
    print(f"\n{len(fails)} failed" if fails else "\nAll passed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
