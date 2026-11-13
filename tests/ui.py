#!/usr/bin/env python3
"""Checks what the pointer does in the window: the real app, with a real pointer and
keyboard, as a user's hand would work them.

Usage: tests/ui.py BINARY

Checked, on the Output's Equalizer page unless it says otherwise:

  graph    a double-click on an empty spot adds a band there, a double-click on a point
           resets its gain (also when the pointer moves a little between the two
           presses), and dragging a point still moves it
  values   a click on the number beside a slider opens it for typing, and what's typed
           is taken. With one number left open, a click on another opens that one, and
           it stays open. A click on the graph then gives the graph the keyboard
           With a number open, a click on another band's point leaves that band as
           it was, and a number typed but not entered goes to the band it was typed for
  sliders  a double-click on a slider returns it to its default, where a single click
           moves it to the pointer
  typing   a number past the slider's range is brought to its end, a minus sign and a
           "k" for thousands are understood, Escape and text that isn't a number change
           nothing
  open     with a number typed but not entered, removing the band leaves the other bands
           alone. With a number open, Reset to Defaults isn't undone when it closes
  effect   on the Compressor's page: a click and a double-click on a slider, and typing
           a number
  mic      on the Microphone's tab: a double-click on a point, a number, and a low-pass
           band, whose Gain slider is off and takes no double-click while its Width does

Not checked: the app with a chain on, a window that isn't maximized, and a touchpad or
a touchscreen.

The accessibility actions the other tests use can't show these: they press a button
without a pointer, and these are about what a press, a second press and the focus do.
So the pointer and the keys are sent through the desktop itself, with GNOME's
remote-desktop interface (org.gnome.Mutter.RemoteDesktop). It needs a GNOME session,
and it stops with a message on any other desktop.

It uses a fresh temporary config with both chains off, so no sound is touched and your
own settings are left alone. The window opens on the primary monitor and is maximized,
and for about two minutes the pointer and the keyboard are the test's: don't use
them, and keep the window on screen and uncovered. Quit Equalizer first, as only one
copy can run.

Where things are in the window is read through accessibility (AT-SPI). Those positions
can be off by a few pixels, and where the window is on screen isn't known, so the first
double-click also measures both: the band it adds says exactly where the pointer was.
"""
import json, math, os, shutil, subprocess, sys, tempfile, time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, Gio, GLib

APP_ID = "io.github.zoeruda.Equalizer"
REMOTE = "org.gnome.Mutter.RemoteDesktop"
CAST = "org.gnome.Mutter.ScreenCast"
DISPLAYS = "org.gnome.Mutter.DisplayConfig"
# The graph's own numbers, from src/eq_graph.rs and src/dsp.rs.
FREQ_MIN, FREQ_MAX, GAIN_LIMIT, AXIS_H = 20.0, 20000.0, 24.0, 22.0
# Linux key codes.
KEYS = {"1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11, "enter": 28, "up": 103,
        "escape": 1, "k": 37, "minus": 74}  # the keypad's minus, which every layout has
LEFT_BUTTON = 272

fails = []


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


# ---- The pointer and the keyboard ----


class Hands:
    """A pointer and a keyboard on the primary monitor, through Mutter. Positions are
    given in the monitor's logical pixels, as a window's are."""

    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.session = None
        _, monitors, logical, _ = self.call(DISPLAYS, "/" + DISPLAYS.replace(".", "/"), DISPLAYS, "GetCurrentState")
        primary = next((m for m in logical if m[4]), None)
        if not primary:
            raise RuntimeError("no primary monitor")
        self.scale = primary[2]
        connector = primary[5][0][0]
        modes = next(m[1] for m in monitors if m[0][0] == connector)
        current = next(m for m in modes if m[6].get("is-current"))
        # The width a maximized window has. A turned monitor swaps the two.
        self.width = round((current[2] if primary[3] in (1, 3, 5, 7) else current[1]) / self.scale)
        self.session = self.call(REMOTE, "/" + REMOTE.replace(".", "/"), REMOTE, "CreateSession")[0]
        ident = self.call(REMOTE, self.session, "org.freedesktop.DBus.Properties", "Get", GLib.Variant("(ss)", (REMOTE + ".Session", "SessionId")))[0]
        cast = self.call(CAST, "/" + CAST.replace(".", "/"), CAST, "CreateSession", GLib.Variant("(a{sv})", ({"remote-desktop-session-id": GLib.Variant("s", ident)},)))[0]
        # A pointer can only be put at a place on something that's being recorded.
        self.stream = self.call(CAST, cast, CAST + ".Session", "RecordMonitor", GLib.Variant("(sa{sv})", (connector, {})))[0]
        self.call(REMOTE, self.session, REMOTE + ".Session", "Start")

    def call(self, dest, path, iface, method, args=None):
        return self.bus.call_sync(dest, path, iface, method, args, None, Gio.DBusCallFlags.NONE, 5000, None).unpack()

    def send(self, method, args):
        self.call(REMOTE, self.session, REMOTE + ".Session", method, args)

    def move(self, x, y):
        self.send("NotifyPointerMotionAbsolute", GLib.Variant("(sdd)", (self.stream, x * self.scale, y * self.scale)))

    def button(self, down):
        self.send("NotifyPointerButton", GLib.Variant("(ib)", (LEFT_BUTTON, down)))

    def go_to(self, x, y):
        """Arrives from just beside it, as a hand does, so the window sees it come."""
        self.move(x - 3, y - 2)
        sleep(0.05)
        self.move(x, y)
        sleep(0.25)

    def click(self, x, y, times=1, shift=0.0):
        """Clicks there. With a shift, the pointer moves that far between the presses."""
        self.go_to(x, y)
        for i in range(times):
            if i and shift:
                self.move(x + shift, y + shift)
                sleep(0.03)
            self.button(True)
            sleep(0.06)
            self.button(False)
            sleep(0.09)

    def drag(self, x, y, dx, dy):
        self.go_to(x, y)
        self.button(True)
        for i in range(1, 11):
            sleep(0.03)
            self.move(x + dx * i / 10, y + dy * i / 10)
        sleep(0.1)
        self.button(False)

    def type(self, *keys):
        for k in keys:
            for down in (True, False):
                self.send("NotifyKeyboardKeycode", GLib.Variant("(ub)", (KEYS[k], down)))
                sleep(0.04)

    def stop(self):
        if self.session:
            try:
                # Nothing is left held down, whatever stopped the test.
                self.button(False)
                self.send("Stop", None)
            except GLib.Error:
                pass


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

    walk(root, 0)
    return found


def box(obj):
    e = obj.get_extents(Atspi.CoordType.WINDOW)
    return e.x, e.y, e.width, e.height


class Window:
    def __init__(self, pid, hands):
        self.hands = hands
        self.root = wait_for(lambda: ui_app(pid), timeout=10)
        if not self.root:
            raise RuntimeError("the window isn't reachable through accessibility (AT-SPI)")
        kids = lambda: [self.root.get_child_at_index(i) for i in range(self.root.get_child_count())]
        self.frame = wait_for(lambda: next((c for c in kids() if c and c.get_role_name() == "frame"), None), timeout=5)
        if not self.frame:
            raise RuntimeError("the window didn't show")
        sleep(1.0)
        self.maximize()
        self.graph  # fails here if it isn't showing
        # How far the pointer must be sent from where accessibility says a thing is.
        # Measured by the first double-click on the graph.
        self.offset = (0.0, 0.0)

    @property
    def graph(self):
        """The showing tab's graph."""
        return self.find(lambda o: o.get_role_name() == "frame" and o.get_name().startswith("Equalizer graph"), "the graph")

    def middle(self, obj, part=0.5):
        """A place on something: across it by `part`, and halfway down."""
        x, y, w, h = box(obj)
        return self.screen(x + w * part, y + h / 2)

    def slider(self, name, part):
        return self.middle(self.find(lambda o: o.get_role_name() == "slider" and o.get_name() == name, f"the {name} slider"), part)

    def button(self, name):
        return self.find(lambda o: o.get_role_name() == "button" and o.get_name() == name, f"the button “{name}”")

    def empty_spot(self):
        """A place on the graph with no point near it. One click there adds nothing."""
        w, h = self.graph_size()
        x, y, _, _ = box(self.graph)
        return self.screen(x + w * 0.75, y + h * 0.8)

    def tab(self, name):
        tab = self.find(lambda o: o.get_role_name() == "page tab" and o.get_name() == name, f"the {name} tab")
        action = tab.get_action_iface()
        if not action or not action.do_action(0) or not wait_for(lambda: tab.get_state_set().contains(Atspi.StateType.SELECTED), timeout=3):
            raise RuntimeError(f"the {name} tab didn't open")
        sleep(0.8)

    def page(self, row):
        """Shows a page by selecting its row in the showing tab's sidebar."""
        def select():
            lists = [l for l in ui_all(self.root, lambda o: o.get_role_name() == "list") if l.get_child_count() == 5 and l.get_selection_iface()]
            if len(lists) != 1:
                return False
            sel = lists[0].get_selection_iface()
            try:
                return sel.is_child_selected(row) or (sel.select_child(row) and sel.is_child_selected(row))
            except GLib.Error:
                return False

        if not wait_for(select, timeout=4):
            raise RuntimeError(f"couldn't select row {row} in the sidebar")
        sleep(0.8)

    def find(self, match, what):
        hits = wait_for(lambda: ui_all(self.root, match), timeout=4)
        if not hits:
            raise RuntimeError(f"couldn't find {what}")
        return hits[0]

    def maximize(self):
        """A maximized window starts at the monitor's left edge, with no shadow around
        it. The desktop maximizes a window that nearly fills the monitor by itself."""
        full = lambda: box(self.frame)[2] == self.hands.width
        if full():
            return
        action = self.frame.get_action_iface()
        names = [action.get_action_name(i) for i in range(action.get_n_actions())] if action else []
        if "window.toggle-maximized" in names:
            action.do_action(names.index("window.toggle-maximized"))
        if not wait_for(full, timeout=3):
            raise RuntimeError("couldn't maximize the window: is it on the primary monitor?")
        sleep(0.5)

    def graph_size(self):
        _, _, w, h = box(self.graph)
        return w, h - AXIS_H

    def graph_point(self, freq, gain):
        """Where a band's point is, as accessibility would give it."""
        x, y, _, _ = box(self.graph)
        w, h = self.graph_size()
        return x + math.log(freq / FREQ_MIN) / math.log(FREQ_MAX / FREQ_MIN) * w, y + (0.5 - gain / (2 * GAIN_LIMIT)) * h

    def screen(self, x, y):
        return x + self.offset[0], y + self.offset[1]

    def value(self, name):
        """The middle of the number beside the slider with this name."""
        label = self.find(lambda o: o.get_role_name() == "label" and o.get_name() == name, f"the {name} slider")
        x, y, w, h = box(label.get_parent().get_child_at_index(1))
        return self.screen(x + w / 2, y + h / 2)


# ---- The app ----


def dbus_pid():
    try:
        out = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path", "/org/freedesktop/DBus",
             "--method", "org.freedesktop.DBus.GetConnectionUnixProcessID", APP_ID],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return int(out.split("uint32 ")[1].split(",")[0]) if "uint32 " in out else None


def sound(effects, more=()):
    return {
        "bands": [{"kind": "Bell", "freq": 1000.0, "gain": 6.0, "q": 1.0, "enabled": True}, *more],
        "preamp": 0.0,
        "balance": 0.0,
        "eq_enabled": True,
        "effects": {e: {"enabled": False, "params": {}} for e in effects},
    }


# The Microphone's second band. Its gain does nothing, and is there to see that it stays.
LOW_PASS = {"kind": "LowPass", "freq": 8000.0, "gain": 3.0, "q": 2.0, "enabled": True}


class App:
    """The app with both chains off, so it makes no devices and touches no sound."""

    def __init__(self, binary, tmp):
        cfg = os.path.join(tmp, "cfg", "equalizer")
        os.makedirs(cfg)
        self.state_path = os.path.join(cfg, "state.json")
        state = {
            "output": {"enabled": False, "device": None, "preset": "Custom", "sound": sound(["autogain", "compressor", "loudness", "limiter"])},
            "mic": {"enabled": False, "device": None, "preset": "Custom", "sound": sound(["gate", "autogain", "compressor", "limiter"], [LOW_PASS])},
            "run_in_background": False,
            "autostart": False,
        }
        json.dump(state, open(self.state_path, "w"))
        self.err = os.path.join(tmp, "stderr.txt")
        env = dict(os.environ, XDG_CONFIG_HOME=os.path.join(tmp, "cfg"))
        self.p = subprocess.Popen([binary], env=env, stdout=subprocess.DEVNULL, stderr=open(self.err, "w"))
        if wait_for(lambda: self.p.poll() is not None, timeout=1.0):
            raise RuntimeError("the app exited right away: is another copy running?")
        self.pid = self.p.pid

    def sound(self, chain="output"):
        try:
            return json.load(open(self.state_path))[chain]["sound"]
        except (OSError, ValueError, KeyError):
            return None

    def saved(self, cond, timeout=5.0, chain="output"):
        """A chain's sound once it's saved as wanted: the app saves a second after a
        change. Otherwise the sound as it was last saved."""
        return wait_for(lambda: (s := self.sound(chain)) and cond(s) and s, timeout=timeout) or self.sound(chain)

    def settled(self, chain="output"):
        """The sound once anything that was going to change it has, and it's saved."""
        sleep(1.8)
        return self.sound(chain)

    def close(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()


def band_near(s, freq):
    """The band nearest a frequency, by ratio."""
    return min(s["bands"], key=lambda b: abs(math.log(b["freq"] / freq)))


# ---- The checks ----


def graph(app, win, hands):
    print("Graph", flush=True)
    w, h = win.graph_size()
    gx, gy, _, _ = box(win.graph)
    # An empty spot, well away from the one point and from the 0 dB line it would snap to.
    sent = (gx + w * 0.25, gy + h * 0.25)
    hands.click(*sent, times=2)
    s = app.saved(lambda s: len(s["bands"]) == 2)
    if not report(len(s["bands"]) == 2, f"a double-click on an empty spot adds a band ({len(s['bands'])} bands)"):
        raise RuntimeError("without that band, where the pointer lands isn't known. Is the window hidden or covered, or was the pointer moved?")
    added = s["bands"][-1]
    # The new band says where in the graph the pointer really was.
    landed = win.graph_point(added["freq"], added["gain"])
    win.offset = (sent[0] - landed[0], sent[1] - landed[1])
    print(f"       on screen, things are {win.offset[0]:+.1f}, {win.offset[1]:+.1f} px from where accessibility puts them in the window")
    if max(abs(win.offset[0]), abs(win.offset[1])) > 0.2 * min(w, h):
        raise RuntimeError("the pointer lands too far off to trust the rest")

    hands.click(*win.screen(*win.graph_point(1000.0, 6.0)), times=2)
    s = app.saved(lambda s: band_near(s, 1000.0)["gain"] == 0.0)
    b = band_near(s, 1000.0)
    report(len(s["bands"]) == 2 and b["gain"] == 0.0 and abs(b["freq"] / 1000.0 - 1) < 0.02,
           f"a double-click on a point resets its gain ({b['gain']:+.1f} dB at {b['freq']:.0f} Hz, {len(s['bands'])} bands)")

    # A hand moves a little between the two presses. That mustn't drag the gain back.
    hands.click(*win.screen(*win.graph_point(added["freq"], added["gain"])), times=2, shift=1.5)
    s = app.saved(lambda s: band_near(s, added["freq"])["gain"] == 0.0)
    b = band_near(s, added["freq"])
    report(len(s["bands"]) == 2 and b["gain"] == 0.0, f"and when the pointer moves between the presses ({b['gain']:+.1f} dB, {len(s['bands'])} bands)")

    # From wherever the point is now, so this doesn't fail only because a check above did.
    # Up by 6 dB and right by a tenth of the graph, which is a factor of 10^0.3.
    was = band_near(s, 1000.0)
    hands.drag(*win.screen(*win.graph_point(was["freq"], was["gain"])), w * 0.1, -h * 6.0 / (2 * GAIN_LIMIT))
    freq, gain = was["freq"] * (FREQ_MAX / FREQ_MIN) ** 0.1, was["gain"] + 6.0
    near = lambda s: abs(band_near(s, freq)["gain"] - gain) <= 0.3 and abs(band_near(s, freq)["freq"] / freq - 1) < 0.03
    s = app.saved(near)
    b = band_near(s, freq)
    report(near(s) and len(s["bands"]) == 2, f"dragging a point moves it ({b['freq']:.0f} Hz {b['gain']:+.1f} dB, wanted {freq:.0f} Hz {gain:+.1f} dB)")
    return b["freq"]


def values(app, win, hands, freq):
    """`freq` is where the first band was dragged to. It's the selected one."""
    print("Values", flush=True)
    hands.click(*win.value("Gain"))
    sleep(0.5)
    hands.type("3", "enter")
    s = app.saved(lambda s: band_near(s, freq)["gain"] == 3.0)
    report(band_near(s, freq)["gain"] == 3.0, f"a click on a number opens it, and what's typed is taken (gain {band_near(s, freq)['gain']:+.1f} dB, typed 3)")

    # Left open, as it is whenever the next click isn't on something that takes the keyboard.
    hands.click(*win.value("Gain"))
    sleep(0.5)
    hands.click(*win.value("Preamp"))
    # Longer than the moment GTK waits before it closes the first one.
    sleep(0.6)
    hands.type("4", "enter")
    s = app.saved(lambda s: s["preamp"] == 4.0)
    report(s["preamp"] == 4.0 and band_near(s, freq)["gain"] == 3.0,
           f"with one number open, a click on another opens it to stay (preamp {s['preamp']:+.1f} dB, typed 4; gain {band_near(s, freq)['gain']:+.1f} dB)")

    hands.click(*win.value("Preamp"))
    sleep(0.5)
    hands.click(*win.empty_spot())
    sleep(0.6)
    hands.type("up")
    was = band_near(s, freq)["gain"]
    s = app.saved(lambda s: band_near(s, freq)["gain"] == was + 0.5)
    report(band_near(s, freq)["gain"] == was + 0.5 and len(s["bands"]) == 2,
           f"with a number open, a click on the graph gives it the keyboard (gain {band_near(s, freq)['gain']:+.1f} dB after the Up key, from {was:+.1f})")

    # The other band, which the first double-click added. The pointer doesn't move between
    # the press and the release, or the drag that starts would hide what the press did.
    mine = dict(band_near(s, freq))
    other = dict(max(s["bands"], key=lambda b: abs(math.log(b["freq"] / freq))))
    point = lambda b: win.screen(*win.graph_point(b["freq"], b["gain"]))
    same = lambda a, b: all(abs(a[k] - b[k]) < 1e-6 for k in ("freq", "gain", "q"))
    for name in ("Gain", "Frequency"):
        hands.click(*win.value(name))
        sleep(0.5)
        hands.click(*point(other))
        sleep(1.6)
        s = app.saved(lambda s: False, timeout=0.5)
        now = band_near(s, other["freq"])
        report(same(now, other) and same(band_near(s, freq), mine),
               f"with the {name} number open, a click on another band's point changes neither band (that band: {now['freq']:.0f} Hz {now['gain']:+.1f} dB, was {other['freq']:.0f} Hz {other['gain']:+.1f} dB)")
        hands.click(*point(mine))
        sleep(0.5)

    hands.click(*win.value("Gain"))
    sleep(0.5)
    hands.type("5")
    hands.click(*point(other))
    s = app.saved(lambda s: band_near(s, freq)["gain"] == 5.0)
    now = band_near(s, other["freq"])
    report(band_near(s, freq)["gain"] == 5.0 and same(now, other),
           f"a number typed but not entered goes to the band it was typed for (its gain {band_near(s, freq)['gain']:+.1f} dB, typed 5; the clicked band's {now['gain']:+.1f} dB, was {other['gain']:+.1f})")
    # The sliders are checked on the first band, so it's selected again, at its new gain.
    hands.click(*win.screen(*win.graph_point(mine["freq"], 5.0)))
    sleep(0.5)


def sliders(app, win, hands, freq):
    print("Sliders", flush=True)
    # Well to the right of every default, so it's the reset that's seen and not the jump.
    hands.click(*win.slider("Preamp", 0.8))
    s = app.saved(lambda s: s["preamp"] > 8.0)
    report(s["preamp"] > 8.0, f"a click on a slider moves it to the pointer (preamp {s['preamp']:+.1f} dB)")
    sleep(0.6)  # so the next press isn't that click's second
    for name, read, default, unit in (
        ("Preamp", lambda s: s["preamp"], 0.0, "dB"),
        ("Balance", lambda s: s["balance"], 0.0, "%"),
        ("Gain", lambda s: band_near(s, freq)["gain"], 0.0, "dB"),
        ("Width (Q)", lambda s: band_near(s, freq)["q"], 1.0, "Q"),
        # Last, as it moves the band the others are read from.
        ("Frequency", lambda s: band_near(s, 1000.0)["freq"], 1000.0, "Hz"),
    ):
        hands.click(*win.slider(name, 0.8), times=2)
        s = app.saved(lambda s: read(s) == default)
        report(read(s) == default and len(s["bands"]) == 2, f"a double-click on the {name} slider resets it ({read(s):.2f} {unit}, default {default:.2f})")
        sleep(0.6)
    return 1000.0


def typing(app, win, hands, freq):
    """Returns where the selected band's frequency is left."""
    print("Typing", flush=True)
    gain = lambda s: band_near(s, freq)["gain"]

    def typed(name, keys, read, want, text):
        hands.click(*win.value(name))
        sleep(0.5)
        hands.type(*keys)
        s = app.saved(lambda s: read(s) == want)
        report(read(s) == want and len(s["bands"]) == 2, f"{text} ({name} {read(s):g}, wanted {want:g})")
        sleep(0.3)

    def unchanged(name, keys, read, text):
        was = read(app.sound())
        hands.click(*win.value(name))
        sleep(0.5)
        hands.type(*keys)
        s = app.settled()
        report(read(s) == was and len(s["bands"]) == 2, f"{text} ({name} {read(s):g}, was {was:g})")

    typed("Gain", ("9", "9", "enter"), gain, GAIN_LIMIT, "a number past the range is brought to its end: 99 typed")
    typed("Gain", ("minus", "7", "enter"), gain, -7.0, "a minus sign is understood: -7 typed")
    unchanged("Gain", ("9", "escape"), gain, "Escape drops what's typed: 9 typed")
    unchanged("Gain", ("k", "enter"), gain, "text that isn't a number changes nothing: k typed")
    # The frequency is read from whichever band is nearest what's wanted.
    typed("Frequency", ("5", "enter"), lambda s: band_near(s, FREQ_MIN)["freq"], FREQ_MIN, "a number below the range is brought to its start: 5 typed")
    typed("Frequency", ("2", "k", "enter"), lambda s: band_near(s, 2000.0)["freq"], 2000.0, "a k means thousands: 2k typed")
    return 2000.0


def left_open(app, win, hands, freq):
    print("Open numbers", flush=True)
    s = app.sound()
    other = dict(max(s["bands"], key=lambda b: abs(math.log(b["freq"] / freq))))
    hands.click(*win.value("Gain"))
    sleep(0.5)
    hands.type("9")
    # With the pointer, as a hand would: the button takes the focus as it's pressed.
    hands.click(*win.middle(win.button("Remove this band")))
    sleep(0.5)
    hands.click(*win.empty_spot())
    s = app.settled()
    left = s["bands"][0] if s["bands"] else {}
    report(len(s["bands"]) == 1 and all(abs(left[k] - other[k]) < 1e-6 for k in ("freq", "gain", "q")),
           f"a number typed for a band that's then removed changes no other ({len(s['bands'])} band left, {left.get('gain', 0):+.1f} dB, was {other['gain']:+.1f} dB; 9 typed)")

    hands.click(*win.value("Preamp"))
    sleep(0.5)
    hands.type("4", "enter")
    s = app.saved(lambda s: s["preamp"] == 4.0)
    if not report(s["preamp"] == 4.0, f"the preamp is set for the next check ({s['preamp']:+.1f} dB, typed 4)"):
        return
    hands.click(*win.value("Preamp"))
    sleep(0.5)
    # Without the pointer, so the number stays open across the reset: the focus doesn't move.
    action = win.button("Reset to Defaults").get_action_iface()
    if not action or not action.do_action(0):
        raise RuntimeError("couldn't press Reset to Defaults")
    s = app.saved(lambda s: s["preamp"] == 0.0 and len(s["bands"]) == 5)
    report(s["preamp"] == 0.0 and len(s["bands"]) == 5, f"Reset to Defaults resets with a number open (preamp {s['preamp']:+.1f} dB, {len(s['bands'])} bands)")
    hands.click(*win.empty_spot())
    s = app.settled()
    report(s["preamp"] == 0.0 and len(s["bands"]) == 5, f"and the number closing doesn't undo it (preamp {s['preamp']:+.1f} dB, {len(s['bands'])} bands)")


def effect(app, win, hands):
    print("Effect page (the Output's Compressor)", flush=True)
    win.page(1)
    param = lambda name: lambda s: s["effects"]["compressor"]["params"].get(name)
    threshold, ratio = param("threshold"), param("ratio")
    try:
        hands.click(*win.slider("Threshold", 0.8))
        s = app.saved(lambda s: (threshold(s) or -60.0) > -15.0)
        report((threshold(s) or -60.0) > -15.0, f"a click on a slider moves it to the pointer (threshold {threshold(s)} dB)")
        sleep(0.6)
        hands.click(*win.slider("Threshold", 0.8), times=2)
        s = app.saved(lambda s: threshold(s) == -18.0)
        report(threshold(s) == -18.0, f"a double-click on the Threshold slider resets it ({threshold(s)} dB, default -18.0)")
        sleep(0.6)
        hands.click(*win.value("Ratio"))
        sleep(0.5)
        hands.type("8", "enter")
        s = app.saved(lambda s: ratio(s) == 8.0)
        report(ratio(s) == 8.0, f"a number typed is taken (ratio {ratio(s)}:1, typed 8)")
        hands.click(*win.slider("Ratio", 0.8), times=2)
        s = app.saved(lambda s: ratio(s) == 3.0)
        report(ratio(s) == 3.0 and threshold(s) == -18.0, f"a double-click on the Ratio slider resets it ({ratio(s)}:1, default 3.0)")
    finally:
        win.page(2)


def mic(app, win, hands):
    print("Microphone tab", flush=True)
    win.tab("Microphone")
    saved = lambda cond: app.saved(cond, chain="mic")
    bell = lambda s: band_near(s, 1000.0)
    low = lambda s: band_near(s, LOW_PASS["freq"])
    try:
        hands.click(*win.screen(*win.graph_point(1000.0, 6.0)), times=2)
        s = saved(lambda s: bell(s)["gain"] == 0.0)
        report(bell(s)["gain"] == 0.0 and len(s["bands"]) == 2, f"a double-click on a point resets its gain ({bell(s)['gain']:+.1f} dB, {len(s['bands'])} bands)")
        sleep(0.6)

        hands.click(*win.value("Preamp"))
        sleep(0.5)
        hands.type("2", "enter")
        s = saved(lambda s: s["preamp"] == 2.0)
        out = app.sound()
        report(s["preamp"] == 2.0 and out["preamp"] == 0.0, f"a number typed is taken, by the Microphone only (its preamp {s['preamp']:+.1f} dB, typed 2; the Output's {out['preamp']:+.1f} dB)")

        # A low-pass has no gain, and rests on the 0 dB line.
        hands.click(*win.screen(*win.graph_point(LOW_PASS["freq"], 0.0)))
        sleep(0.8)
        hands.click(*win.slider("Width (Q)", 0.8), times=2)
        s = saved(lambda s: low(s)["q"] == 1.0)
        report(low(s)["q"] == 1.0 and len(s["bands"]) == 2, f"a low-pass band's Width slider takes a double-click (Q {low(s)['q']:.2f}, was {LOW_PASS['q']:.2f}, default 1.00)")
        sleep(0.6)
        hands.click(*win.slider("Gain", 0.8), times=2)
        s = app.settled("mic")
        report(low(s)["gain"] == LOW_PASS["gain"] and bell(s)["gain"] == 0.0 and len(s["bands"]) == 2,
               f"its Gain slider is off, and takes none (its gain {low(s)['gain']:+.1f} dB, was {LOW_PASS['gain']:+.1f}; the other band's {bell(s)['gain']:+.1f} dB)")
    finally:
        win.tab("Output")


def main():
    if len(sys.argv) != 2 or sys.argv[1].startswith("-"):
        sys.exit(__doc__.split("\n\n")[1])
    binary = os.path.abspath(sys.argv[1])
    if not os.access(binary, os.X_OK):
        sys.exit(f"Not an executable: {binary}")
    if not shutil.which("gdbus"):
        sys.exit("gdbus is missing.")
    if dbus_pid():
        sys.exit("Equalizer is running. Quit it first, as only one copy can run.")
    try:
        hands = Hands()
    except (GLib.Error, RuntimeError, StopIteration) as e:
        sys.exit(f"This test sends its pointer and keys through GNOME (org.gnome.Mutter.RemoteDesktop), which isn't here: {getattr(e, 'message', e)}")
    tmp = tempfile.mkdtemp(prefix="equi")
    app = None
    print("Don't touch the pointer or the keyboard, and keep the Equalizer window uncovered, until this finishes.\n", flush=True)
    try:
        app = App(binary, tmp)
        win = Window(app.pid, hands)
        freq = graph(app, win, hands)
        values(app, win, hands, freq)
        freq = sliders(app, win, hands, freq)
        freq = typing(app, win, hands, freq)
        left_open(app, win, hands, freq)
        effect(app, win, hands)
        mic(app, win, hands)
    except (RuntimeError, GLib.Error) as e:
        report(False, f"stopped: {getattr(e, 'message', e)}")
    finally:
        hands.stop()
        if app:
            app.close()
            messages = open(app.err, errors="replace").read()
            if fails and messages:
                print("\nThe app's messages:\n" + messages[-3000:])
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(fails)} failed" if fails else "\nAll passed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
