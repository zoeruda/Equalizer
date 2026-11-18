#!/usr/bin/env python3
"""Checks what the pointer does in the window: the real app, with a real pointer and
keyboard, as a user's hand would work them.

Usage: tests/ui.py BINARY [--random [--seed N] [--steps N] [--until K]]

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

  keyboard Tab, Up, Right, Delete, plus and Escape on the graph, with no pointer once it
           has the focus
  window   the graph's checks again in a window that isn't maximized, and the sliders and
           typing in the smallest one
  chain    with the Output on a silent device: a point dragged in the window changes
           the sound by what it should
  plays    "Plays through" picked in the dropdown (the list opened with the pointer, the
           entry chosen with the arrow keys): it's saved, the Output moves to that device,
           and the dropdown shows it
  printed  in every part, the app prints no GTK warning or critical, and no panic

With --random it does random clicks, double-clicks, drags, scrolls, typed text and keys
on the graph, the sliders, the numbers, the pages and both tabs, from a seed, and after
each one checks that the app is running and printed nothing, that state.json is in range,
that only one thing changed (and nothing on the other tab), and that the window's Preamp
and Balance sliders show what's saved. A failure prints the seed: run it again with
`--seed N --until K`.

Not checked: a touchpad or a touchscreen.

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
import argparse, json, math, os, random, re, shutil, subprocess, sys, tempfile, time

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
        "escape": 1, "k": 37, "minus": 74,  # the keypad's minus, which every layout has
        "down": 108, "left": 105, "right": 106, "tab": 15, "delete": 111, "plus": 78}  # the keypad's plus
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

    def scroll(self, x, y, steps):
        """Turns the wheel over a place: positive steps go down."""
        self.go_to(x, y)
        for _ in range(abs(steps)):
            self.send("NotifyPointerAxisDiscrete", GLib.Variant("(ui)", (0, 1 if steps > 0 else -1)))
            sleep(0.15)

    window = None  # set by Window, so that keys are only sent while it has the focus

    def require_focus(self):
        """Stops the test if the Equalizer window isn't the one with the focus: keys sent
        then would go to whatever has it, such as the terminal this was started from."""
        if self.window is not None and not self.window.frame.get_state_set().contains(Atspi.StateType.ACTIVE):
            raise RuntimeError("the Equalizer window doesn't have the focus, so the keys that would be sent now would go elsewhere")

    def type(self, *keys):
        self.require_focus()
        for k in keys:
            for down in (True, False):
                self.send("NotifyKeyboardKeycode", GLib.Variant("(ub)", (KEYS[k], down)))
                sleep(0.04)

    def key_code(self, code, down):
        if code == 56 and down:
            self.require_focus()
        self.send("NotifyKeyboardKeycode", GLib.Variant("(ub)", (code, down)))
        sleep(0.012)

    def shrink_window(self, steps=220):
        """Makes the window as small as it goes, with the desktop's keyboard resize (Alt+F8):
        the first arrow key picks that edge and each key after moves it, about 6 pixels.
        Width and height are done one after the other, as two edges at once go the wrong
        way. The window's top left corner stays where it is."""
        for edge, back in ((106, 105), (108, 103)):  # right edge, moved left; bottom edge, moved up
            self.key_code(56, True)  # Alt
            self.key_code(66, True)  # F8
            self.key_code(66, False)
            self.key_code(56, False)
            sleep(0.4)
            for code in (edge,) + (back,) * steps + (28,):
                self.key_code(code, True)
                self.key_code(code, False)
            sleep(0.6)

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
    def __init__(self, pid, hands, maximize=True):
        self.hands = hands
        hands.window = self
        self.root = wait_for(lambda: ui_app(pid), timeout=10)
        if not self.root:
            raise RuntimeError("the window isn't reachable through accessibility (AT-SPI)")
        kids = lambda: [self.root.get_child_at_index(i) for i in range(self.root.get_child_count())]
        self.frame = wait_for(lambda: next((c for c in kids() if c and c.get_role_name() == "frame"), None), timeout=5)
        if not self.frame:
            raise RuntimeError("the window didn't show")
        sleep(1.0)
        if maximize:
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

    def visible_part(self, obj):
        """The top and bottom of what can be seen of this: the window, and inside a scroller
        only what's in the scroller. Also gives that scroller, if any."""
        fx, fy, fw, fh = box(self.frame)
        up, pane = obj.get_parent(), None
        for _ in range(12):
            if up is None:
                break
            if up.get_role_name() == "scroll pane":
                pane = up
                sx, sy, sw, sh = box(up)
                top, bottom = max(fy, sy), min(fy + fh, sy + sh)
                fy, fh = top, bottom - top
                break
            up = up.get_parent()
        return fy, fy + fh, pane

    def into_view(self, obj):
        """In a small window the controls are in a scroller and some are below its edge:
        scrolls this one into view with the scroller's bar. Raises if it can't, so
        that a click isn't sent to somewhere that isn't the control."""
        for attempt in range(8):
            _, y, _, h = box(obj)
            top, bottom, pane = self.visible_part(obj)
            if y >= top and y + h <= bottom:
                return
            if pane is None:
                break
            # The scroller's own bar can be set through accessibility. (The wheel over it would
            # change the slider under the pointer, and the focus doesn't scroll.)
            bar = next((pane.get_child_at_index(i) for i in range(pane.get_child_count())
                        if pane.get_child_at_index(i).get_role_name() == "scroll bar"), None)
            if bar is None:
                break
            value = bar.get_value_iface()
            now = Atspi.Value.get_current_value(value)
            move = (y + h - bottom) if y + h > bottom else (y - top)
            Atspi.Value.set_current_value(value, max(Atspi.Value.get_minimum_value(value), min(Atspi.Value.get_maximum_value(value), now + move)))
            sleep(0.6)
        raise RuntimeError(f"couldn't scroll “{obj.get_name()}” into view")

    def slider(self, name, part):
        obj = self.find(lambda o: o.get_role_name() == "slider" and o.get_name() == name, f"the {name} slider")
        self.into_view(obj)
        return self.middle(obj, part)

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

    def current(self):
        """The tab and the page showing, read from the window: keys can change both (the
        arrow keys on the tab strip or the sidebar do), as well as clicks."""
        tab = next((o.get_name() for o in ui_all(self.root, lambda o: o.get_role_name() == "page tab")
                    if o.get_state_set().contains(Atspi.StateType.SELECTED)), None)
        row = None
        lists = [l for l in ui_all(self.root, lambda o: o.get_role_name() == "list") if l.get_child_count() == 5 and l.get_selection_iface()]
        if len(lists) == 1:
            sel = lists[0].get_selection_iface()
            row = next((i for i in range(5) if sel.is_child_selected(i)), None)
        return tab, row

    def slider_names(self):
        return sorted({o.get_name() for o in ui_all(self.root, lambda o: o.get_role_name() == "slider") if o.get_name()})

    def slider_value(self, name):
        """What a slider shows, or None if it isn't there."""
        hits = ui_all(self.root, lambda o: o.get_role_name() == "slider" and o.get_name() == name)
        if not hits:
            return None
        try:
            return Atspi.Value.get_current_value(hits[0].get_value_iface())
        except (GLib.Error, AttributeError, TypeError):
            return None

    def value(self, name):
        """The middle of the number beside the slider with this name."""
        label = self.find(lambda o: o.get_role_name() == "label" and o.get_name() == name, f"the {name} slider")
        self.into_view(label)
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

    def __init__(self, binary, tmp, output_device=None):
        cfg = os.path.join(tmp, "cfg", "equalizer")
        os.makedirs(cfg, exist_ok=True)
        self.state_path = os.path.join(cfg, "state.json")
        state = {
            "output": {"enabled": output_device is not None, "device": output_device, "preset": "Custom", "sound": sound(["autogain", "compressor", "loudness", "limiter"])},
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


# ---- Lines the app prints ----

# Lines that are known and fine. GTK's own warnings and criticals are not: they mean a widget
# was used wrongly. (Only what this machine's session prints belongs here, never what one
# person's own settings cause.)
KNOWN_LINES = [r"unknown resource \d+ op:7"]


def bad_lines(text):
    return [l for l in text.splitlines()
            if re.search(r"WARNING|CRITICAL|panicked|\*\*\s*ERROR|Gtk-|Gdk-|Adwaita-", l) and not any(re.search(k, l) for k in KNOWN_LINES)]


# ---- More cases ----


def changes(before, after):
    """What differs between two sounds: the indexes of the bands that changed (with the
    band count if it changed), and whether anything else did."""
    dn = len(after["bands"]) - len(before["bands"])
    if dn == 0:
        changed = [i for i in range(len(before["bands"])) if before["bands"][i] != after["bands"][i]]
    else:
        # A band added or removed shifts the ones after it, so match them up by what they are.
        left = list(after["bands"])
        unmatched = []
        for i, b in enumerate(before["bands"]):
            if b in left:
                left.remove(b)
            else:
                unmatched.append(i)
        # Of the bands that have no match, the removed ones aren't changes (the added are in `left`).
        changed = unmatched[max(0, -dn):] if dn < 0 else unmatched
    rest_b = {k: v for k, v in before.items() if k != "bands"}
    rest_a = {k: v for k, v in after.items() if k != "bands"}
    return changed, dn, rest_a != rest_b


def keyboard(app, win, hands):
    print("Keyboard only, on the graph", flush=True)
    # One click for the focus. Single, on an empty spot: it adds nothing.
    hands.click(*win.empty_spot())
    sleep(0.5)
    s0 = app.sound()
    hands.type("plus")
    s1 = app.saved(lambda s: len(s["bands"]) == len(s0["bands"]) + 1)
    report(len(s1["bands"]) == len(s0["bands"]) + 1, f"the plus key adds a band ({len(s0['bands'])} -> {len(s1['bands'])} bands)")
    hands.type("up", "up")
    s2 = app.saved(lambda s: changes(s1, s)[0] != [])
    ch, dn, other = changes(s1, s2)
    gain = s2["bands"][ch[0]]["gain"] - s1["bands"][ch[0]]["gain"] if len(ch) == 1 else None
    report(len(ch) == 1 and dn == 0 and not other and gain == 1.0, f"Up twice raises one band by 1 dB (bands changed: {ch}, gain change {gain})")
    hands.type("tab", "up")
    s3 = app.saved(lambda s: changes(s2, s)[0] != [])
    ch2, dn, other = changes(s2, s3)
    report(len(ch2) == 1 and ch2 != ch and dn == 0 and not other, f"Tab selects another band, and Up raises that one (bands changed: {ch} then {ch2})")
    hands.type("right")
    s4 = app.saved(lambda s: changes(s3, s)[0] != [])
    ch3, dn, other = changes(s3, s4)
    freq_up = len(ch3) == 1 and s4["bands"][ch3[0]]["freq"] > s3["bands"][ch3[0]]["freq"]
    report(freq_up and dn == 0 and not other, f"Right raises the frequency of the selected band (bands changed: {ch3})")
    hands.type("delete")
    s5 = app.saved(lambda s: len(s["bands"]) == len(s4["bands"]) - 1)
    report(len(s5["bands"]) == len(s4["bands"]) - 1, f"Delete removes the selected band ({len(s4['bands'])} -> {len(s5['bands'])} bands)")
    hands.type("tab", "tab", "escape", "up")
    s6 = app.settled()
    ch, dn, _ = changes(s5, s6)
    report(dn == 0 and len(ch) <= 1 and len(s6["bands"]) == len(s5["bands"]), "Escape leaves the graph without adding or removing a band")


def chain_on(binary, tmp, hands):
    print("With the Output on (a silent device)", flush=True)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from common import Tones, null_sink, unload, node_exists

    name = "equi_out"
    module = null_sink(name, "EquiTest")
    sleep(0.5)
    if not node_exists(name):
        report(False, "couldn't make the silent device")
        return
    app = win = None
    try:
        sub = os.path.join(tmp, "chain")
        os.makedirs(sub)
        app = App(binary, sub, output_device=name)
        if not wait_for(lambda: node_exists("equalizer_sink"), timeout=10):
            raise RuntimeError("the app's virtual device didn't appear")
        sleep(1.0)
        win = Window(app.pid, hands)
        freq = graph(app, win, hands)
        tones = Tones(sub)
        level = lambda f: tones.measure(name, "equalizer_sink", freq=f, seconds=0.8) - tones.measure(name, name, freq=f, seconds=0.8)
        s = app.settled()
        b = band_near(s, freq)
        before = level(b["freq"])
        # Down to 0 dB, straight down from where the point is.
        w, h = win.graph_size()
        hands.drag(*win.screen(*win.graph_point(b["freq"], b["gain"])), 0, h * b["gain"] / (2 * GAIN_LIMIT))
        s = app.saved(lambda s: abs(band_near(s, b["freq"])["gain"]) < 0.5)
        moved = band_near(s, b["freq"])
        sleep(0.8)
        after = level(b["freq"])
        want = b["gain"] - moved["gain"]
        report(abs((before - after) - want) <= 1.5,
               f"a band dragged in the window changes the sound ({b['gain']:+.1f} -> {moved['gain']:+.1f} dB at {b['freq']:.0f} Hz: the sound fell {before - after:.1f} dB, wanted {want:.1f})")
    finally:
        if app:
            app.close()
        unload(module)


def live_pick(binary, tmp, hands):
    print("Plays through, picked in the dropdown (the Output on silent devices)", flush=True)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from common import null_sink, unload, node_exists, linked_to, plays_through

    a, b = "equi_a", "equi_b"
    mods = [null_sink(a, "EquiA"), null_sink(b, "EquiB")]
    sleep(0.6)
    app = None
    try:
        if not (node_exists(a) and node_exists(b)):
            report(False, "couldn't make the silent devices")
            return
        sub = os.path.join(tmp, "live")
        os.makedirs(sub)
        app = App(binary, sub, output_device=a)
        if not wait_for(lambda: node_exists("equalizer_sink"), timeout=10):
            raise RuntimeError("the app's virtual device didn't appear")
        sleep(1.0)
        win = Window(app.pid, hands)
        graph(app, win, hands)  # for where the pointer lands
        report(plays_through(win) == "EquiA", f"the dropdown shows the saved device ({plays_through(win)})")

        def pick(desc):
            """Opens the list with the pointer, moves to the entry with the arrow keys, and takes it."""
            shows = plays_through(win)
            button = win.find(lambda o: o.get_role_name() == "toggle button" and o.get_name() == shows, "the Plays through button")
            hands.click(*win.middle(button))
            sleep(0.8)
            rows = {}
            for label in ui_all(win.root, lambda o: o.get_role_name() == "label" and o.get_name() in ("System default", "EquiA", "EquiB")):
                o = label
                for _ in range(4):
                    if o is not None and o.get_role_name() == "list item":
                        rows[label.get_name()] = o.get_index_in_parent()
                        break
                    o = o.get_parent() if o is not None else None
            if desc not in rows or shows not in rows:
                raise RuntimeError(f"the list doesn't show {desc} and {shows} (it shows {sorted(rows)})")
            # Where the keyboard starts in the list isn't known, so go to the top first.
            hands.type(*(["up"] * (len(rows) + 1)), *(["down"] * rows[desc]), "enter")
            sleep(1.2)

        pick("EquiB")
        s2 = app.saved(lambda s: s is not None, chain="output")
        saved = json.load(open(app.state_path))["output"]["device"]
        report(saved == b, f"picking EquiB saves it ({saved})")
        report(bool(wait_for(lambda: linked_to("equalizer_out") == [b], timeout=4)), f"and the Output plays through it ({', '.join(linked_to('equalizer_out')) or 'nothing'})")
        report(plays_through(win) == "EquiB", f"and the dropdown shows it ({plays_through(win)})")
        pick("System default")
        saved = json.load(open(app.state_path))["output"]["device"]
        report(saved is None, f"picking System default saves none ({saved})")
        report(plays_through(win) == "System default", f"and the dropdown shows it ({plays_through(win)})")
    finally:
        if app:
            app.close()
        for m in mods:
            unload(m)


def recalibrate(app, win, hands):
    """Where the pointer lands, measured again, after the window has moved or changed size:
    a double-click on an empty spot adds a band where it really was, and that band is then
    removed with the Delete key."""
    w, h = win.graph_size()
    gx, gy, _, _ = box(win.graph)
    count = len(app.sound()["bands"])
    sent = (gx + w * 0.25, gy + h * 0.25)
    win.offset = (0.0, 0.0)
    hands.click(*sent, times=2)
    s = app.saved(lambda s: len(s["bands"]) == count + 1)
    if len(s["bands"]) != count + 1:
        raise RuntimeError("couldn't measure where the pointer lands in the smaller window")
    added = s["bands"][-1]
    landed = win.graph_point(added["freq"], added["gain"])
    win.offset = (sent[0] - landed[0], sent[1] - landed[1])
    print(f"       in the smaller window, things are {win.offset[0]:+.1f}, {win.offset[1]:+.1f} px from where accessibility puts them", flush=True)
    hands.type("delete")
    s = app.saved(lambda s: len(s["bands"]) == count)
    if len(s["bands"]) != count:
        raise RuntimeError("couldn't take the band used for measuring away again")


def small_window(binary, tmp, hands):
    print("A window that isn't maximized", flush=True)
    sub = os.path.join(tmp, "small")
    os.makedirs(sub)
    app = App(binary, sub)
    try:
        win = Window(app.pid, hands, maximize=False)
        full = lambda: box(win.frame)[2] == hands.width
        if full():
            action = win.frame.get_action_iface()
            names = [action.get_action_name(i) for i in range(action.get_n_actions())]
            if "window.toggle-maximized" in names:
                action.do_action(names.index("window.toggle-maximized"))
            wait_for(lambda: not full(), timeout=3)
            sleep(1.0)
        x, y, wd, ht = box(win.frame)
        print(f"       the window is {wd} x {ht}", flush=True)
        report(wd < hands.width, "the window isn't maximized")
        graph(app, win, hands)
        values(app, win, hands, 2000.0)
        # The smallest the window allows, by the keyboard (a drag on its corner doesn't resize it
        # here). The top left corner stays where it is, so the pointer's offset still holds.
        print("The smallest size the window allows", flush=True)
        x, y, wd, ht = box(win.frame)
        hands.shrink_window()
        sleep(1.0)
        _, _, w2, h2 = box(win.frame)
        if not report(w2 < wd and h2 < ht, f"dragging the corner makes the window smaller ({wd} x {ht} -> {w2} x {h2})"):
            return
        report(w2 >= 300 and h2 >= 300, "and it stops at a size that still has room for the graph")
        recalibrate(app, win, hands)
        # The Gain, Width and Frequency sliders belong to the selected band: take the one at 2 kHz.
        keep = band_near(app.sound(), 2000.0)
        hands.click(*win.screen(*win.graph_point(keep["freq"], keep["gain"])))
        sleep(0.8)
        sliders(app, win, hands, 2000.0)
        typing(app, win, hands, 1000.0)
    finally:
        app.close()


# ---- Random ----

RANGES = {"freq": (10.0, 24_000.0), "gain": (-24.0, 24.0), "q": (0.1, 20.0)}


def in_range(sound):
    """What's wrong with a saved sound, if anything."""
    if len(sound["bands"]) > 32:
        return f"{len(sound['bands'])} bands"
    for b in sound["bands"]:
        for k, (lo, hi) in RANGES.items():
            if not (lo <= b[k] <= hi):
                return f"a band's {k} is {b[k]}"
    if not (-24.0 <= sound["preamp"] <= 20.0):
        return f"preamp {sound['preamp']}"
    if not (-100.0 <= sound["balance"] <= 100.0):
        return f"balance {sound['balance']}"
    return None


def random_run(app, win, hands, seed, steps, until):
    print(f"Random: seed {seed}", flush=True)
    rng = random.Random(seed)
    graph(app, win, hands)  # puts the pointer's offset right, and gives the graph a band to find
    EQ = 2  # the equalizer is the third row of the sidebar on both tabs (src/model.rs, Direction::chain)
    state = {"tab": "Output", "pages": {"Output": EQ, "Microphone": EQ}}
    page = lambda: state["pages"][state["tab"]]  # the graph is on the equalizer's page only
    chain = lambda: "output" if state["tab"] == "Output" else "mic"
    done = []

    noted = []

    def graph_here():
        """Whether the graph is showing. On the page the sidebar says is the EQ's it should
        be; when it isn't, the step is skipped and noted (the sidebar and the page disagree)."""
        if page() != EQ:
            return False
        if ui_all(win.root, lambda o: o.get_role_name() == "frame" and o.get_name().startswith("Equalizer graph")):
            return True
        noted.append(f"step {len(done) + 1}: the sidebar has the EQ page selected on the {state['tab']} tab, but the graph isn't showing")
        return False

    def points():
        s = app.sound(chain())
        return [win.screen(*win.graph_point(b["freq"], b["gain"])) for b in (s["bands"] if s else [])]

    def somewhere():
        w, h = win.graph_size()
        x, y, _, _ = box(win.graph)
        return win.screen(x + rng.uniform(0.05, 0.95) * w, y + rng.uniform(0.05, 0.95) * h)

    def act_point(kind):
        if not graph_here():
            return None
        pts = points()
        if not pts:
            return None
        x, y = rng.choice(pts)
        if kind == "click":
            hands.click(x, y)
            return "click on a point"
        if kind == "double":
            hands.click(x, y, times=2)
            return "double-click on a point"
        w, h = win.graph_size()
        dx, dy = rng.uniform(-0.2, 0.2) * w, rng.uniform(-0.3, 0.3) * h
        hands.drag(x, y, dx, dy)
        return f"drag a point by {dx:.0f}, {dy:.0f}"

    def act_empty():
        if not graph_here():
            return None
        hands.click(*somewhere(), times=rng.choice([1, 1, 2]))
        return "click(s) somewhere on the graph"

    def act_scroll():
        if not graph_here():
            return None
        pts = points()
        x, y = rng.choice(pts) if pts and rng.random() < 0.5 else somewhere()
        n = rng.choice([-3, -1, 1, 3])
        hands.scroll(x, y, n)
        return f"scroll {n} over the graph"

    def act_slider():
        names = win.slider_names()
        if not names:
            return None
        name = rng.choice(names)
        hands.click(*win.slider(name, rng.uniform(0.1, 0.9)), times=rng.choice([1, 2]))
        return f"click(s) on the {name} slider"

    def act_number():
        names = win.slider_names()
        if not names:
            return None
        name = rng.choice(names)
        hands.click(*win.value(name))
        sleep(0.4)
        keys = [rng.choice(["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "k", "minus", "escape"]) for _ in range(rng.randint(1, 4))]
        hands.type(*keys, rng.choice(["enter", "enter", "escape"]))
        return f"typed {' '.join(keys)} into the {name} number"

    def act_keys():
        keys = [rng.choice(["tab", "up", "down", "left", "right", "delete", "plus", "escape"]) for _ in range(rng.randint(1, 4))]
        hands.type(*keys)
        # Each key can change a band (and Tab moves on to another), so up to as many as typed.
        state["keys"] = len([k for k in keys if k in ("up", "down", "left", "right", "delete", "plus")])
        return f"keys {' '.join(keys)}"

    def act_page():
        row = EQ if rng.random() < 0.5 else rng.randrange(5)
        win.page(row)
        state["pages"][state["tab"]] = row
        return f"page {row}"

    def act_tab():
        state["tab"] = "Microphone" if state["tab"] == "Output" else "Output"
        win.tab(state["tab"])
        return f"the {state['tab']} tab"

    actions = [(5, lambda: act_point("click")), (5, lambda: act_point("double")), (6, lambda: act_point("drag")), (3, act_empty),
               (2, act_scroll), (5, act_slider), (4, act_number), (4, act_keys), (2, act_page), (1, act_tab)]
    bag = [a for w_, a in actions for _ in range(w_)]
    log_pos = len(open(app.err, errors="replace").read())
    ok = True
    for k in range(1, (until or steps) + 1):
        before = {c: app.sound(c) for c in ("output", "mic")}
        state["keys"] = None
        try:
            say = rng.choice(bag)()
        except RuntimeError as e:
            shown = sorted({f"{o.get_role_name()}:{o.get_name()}" for o in ui_all(win.root, lambda o: o.get_role_name() in ("frame", "page tab", "list", "slider", "toggle button"))})
            print(f"         {e}\n         tab {state['tab']}, pages {state['pages']}; showing: {', '.join(shown)[:600]}", flush=True)
            raise
        if say is None:
            say = "(nothing to do)"
        sleep(1.5)
        tab, row = win.current()
        if tab:
            state["tab"] = tab
            if row is not None:
                state["pages"][tab] = row
        problems = []
        if app.p.poll() is not None:
            problems.append(f"the app has exited with code {app.p.returncode}")
        text = open(app.err, errors="replace").read()
        fresh, log_pos = text[log_pos:], len(text)
        problems += [f"the app printed: {l}" for l in bad_lines(fresh)]
        after = {c: app.sound(c) for c in ("output", "mic")}
        for c in ("output", "mic"):
            if after[c] is None:
                problems.append(f"state.json is unreadable ({c})")
                continue
            wrong = in_range(after[c])
            if wrong:
                problems.append(f"{c}: out of range: {wrong}")
        if all(after.values()) and all(before.values()):
            shown = chain()
            other = "mic" if shown == "output" else "output"
            if after[other] != before[other]:
                problems.append(f"the {other} chain changed while the {state['tab']} tab was showing")
            changed, dn, _ = changes(before[shown], after[shown])
            if state["keys"] is not None:
                if len(changed) + abs(dn) > max(state["keys"], 1):
                    problems.append(f"more changed than the {state['keys']} keys typed: bands {changed}, band count {dn:+d}")
            elif abs(dn) > 1 or (dn and changed) or len(changed) > 1:
                problems.append(f"more than one thing changed: bands {changed}, band count {dn:+d}")
            # The window shows what's saved: the EQ page's two sliders.
            if page() == EQ and after[shown]:
                # Accessibility gives a slider's position from 0 to 1 across its range.
                for name, key, lo, hi in (("Preamp", "preamp", -24.0, 20.0), ("Balance", "balance", -100.0, 100.0)):
                    v = win.slider_value(name)
                    if v is not None and abs(lo + v * (hi - lo) - after[shown][key]) > 0.01 * (hi - lo):
                        problems.append(f"the {name} slider shows {lo + v * (hi - lo):.1f}, state.json has {after[shown][key]:.1f}")
        done.append((k, say, problems))
        print(f"  {k:>3}. {say}{'  <- FAIL' if problems else ''}", flush=True)
        if problems:
            for pr in problems:
                print(f"         {pr}", flush=True)
                fails.append(f"step {k}: {pr}")
            ok = False
            break
    if not ok:
        print(f"\nRun again: tests/ui.py BINARY --random --seed {seed} --until {done[-1][0]}", flush=True)
    else:
        report(True, f"{steps} random steps, none broke a rule")
    for n in noted:
        print(f"  note {n}", flush=True)


def main():
    ap = argparse.ArgumentParser(usage="tests/ui.py BINARY [--random [--seed N] [--steps N] [--until K]]", add_help=False)
    ap.add_argument("binary")
    ap.add_argument("--random", action="store_true")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--steps", type=int, default=40)
    ap.add_argument("--until", type=int)
    try:
        args = ap.parse_args()
    except SystemExit:
        sys.exit(__doc__.split("\n\n")[1])
    binary = os.path.abspath(args.binary)
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
    messages = ""
    print("Don't touch the pointer or the keyboard, and keep the Equalizer window uncovered, until this finishes.\n", flush=True)

    def section(name, fn, *a, **kw):
        """One part with its own copy of the app, so a failure in one leaves the rest alone."""
        nonlocal messages
        sub = os.path.join(tmp, name)
        os.makedirs(sub, exist_ok=True)
        app = None
        try:
            app = App(binary, sub)
            win = Window(app.pid, hands)
            fn(app, win, hands, *a, **kw)
        except (RuntimeError, GLib.Error) as e:
            report(False, f"{name} stopped: {getattr(e, 'message', e)}")
        finally:
            if app:
                app.close()
                text = open(app.err, errors="replace").read()
                messages += text
                lines = bad_lines(text)
                report(not lines, f"{name}: the app printed no warnings ({lines[0][:120] if lines else 'none'})")

    try:
        if args.random:
            seed = args.seed if args.seed is not None else random.randrange(1 << 30)
            section("random", random_run, seed, args.steps, args.until)
        else:
            def checks(app, win, hands):
                freq = graph(app, win, hands)
                values(app, win, hands, freq)
                freq = sliders(app, win, hands, freq)
                freq = typing(app, win, hands, freq)
                left_open(app, win, hands, freq)
                effect(app, win, hands)
                mic(app, win, hands)

            section("pointer", checks)
            section("keyboard", keyboard)
            small_window(binary, tmp, hands)
            chain_on(binary, tmp, hands)
            live_pick(binary, tmp, hands)
    except (RuntimeError, GLib.Error) as e:
        report(False, f"stopped: {getattr(e, 'message', e)}")
    finally:
        hands.stop()
        if fails and messages:
            print("\nThe app's messages:\n" + messages[-3000:])
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(fails)} failed" if fails else "\nAll passed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
