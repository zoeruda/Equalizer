"""Helpers shared by the newer tests (tests/events.py). measure.py, mic.py and memory.py
keep their own copies for now."""
import json, math, os, re, signal, struct, subprocess, tempfile, time, wave

RATE = 48000
NO_FALLBACK = "{ node.dont-fallback = true node.dont-reconnect = true }"


def run(*a, timeout=20):
    try:
        return subprocess.run(a, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def wait_for(cond, timeout=6.0, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return cond()


# ---- PipeWire ----


def graph():
    """Nodes by id (their props), links as (output node id, input node id), and each
    client's process id."""
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


def linked_to(source_name):
    """Names of the nodes that a node's output goes to."""
    nodes, links, _ = graph()
    ids = {i for i, p in nodes.items() if p.get("node.name") == source_name}
    return sorted({nodes[i].get("node.name") for o, i in links if o in ids and i in nodes})


def null_sink(name, description=None, channels=2):
    """A silent device; returns its module id."""
    return run("pactl", "load-module", "module-null-sink", f"sink_name={name}", f"channels={channels}",
               f"sink_properties=device.description=\"{description or name}\"")


def unload(module):
    if module:
        run("pactl", "unload-module", module)


def sinks():
    try:
        return {s["name"]: s for s in json.loads(run("pactl", "-f", "json", "list", "sinks") or "[]")}
    except json.JSONDecodeError:
        return {}


def sink_volume(name):
    """(per channel volume in dB, muted), or None if there's no such sink."""
    s = sinks().get(name)
    if not s:
        return None
    vols = []
    for c in s.get("volume", {}).values():
        v = c.get("value_percent", "0%").rstrip("%")
        # The percentage is the cube root of the gain (PipeWire's volume scale).
        vols.append(60 * math.log10(float(v) / 100) if float(v) > 0 else -100.0)
    # A mono device's volume lists a second channel that it doesn't have.
    m = re.search(r"(\d+)ch", str(s.get("sample_specification", "")))
    vols = vols[: int(m.group(1)) if m else len(vols)]
    return vols, bool(s.get("mute"))


def default_sink():
    out = run("pactl", "get-default-sink")
    return out or None


def source_outputs(prefix="equalizer_"):
    try:
        items = json.loads(run("pactl", "-f", "json", "list", "source-outputs") or "[]")
    except json.JSONDecodeError:
        return ["<pactl gave no JSON>"]
    names = [str((so.get("properties") or {}).get("node.name", "")) for so in items]
    return sorted(n for n in names if n.startswith(prefix))


# ---- Sound ----


class Tones:
    """Plays tones and measures what a silent device gets."""

    def __init__(self, tmp):
        self.tmp = tmp

    def tone(self, freq, db, seconds, name="tone.wav"):
        path = os.path.join(self.tmp, name)
        amp = 32767 * 10 ** (db / 20)
        with wave.open(path, "wb") as w:
            w.setnchannels(2), w.setsampwidth(2), w.setframerate(RATE)
            data = bytearray()
            for i in range(int(RATE * seconds)):
                s = int(amp * math.sin(2 * math.pi * freq * i / RATE))
                data += struct.pack("<hh", s, s)
            w.writeframes(bytes(data))
        return path

    def level(self, path, freq):
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

    def measure(self, record_from, into, freq=1000, db=-20.0, seconds=1.0):
        """Plays a tone into `into` and records what `record_from` (a silent device) plays."""
        out = os.path.join(self.tmp, "out.wav")
        rec = subprocess.Popen(
            ["pw-record", f"--target={record_from}", "-P", "{stream.capture.sink=true}", "--rate", str(RATE), "--channels", "2", "--format", "s16", out],
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.35)
        subprocess.run(["pw-play", f"--target={into}", "-P", NO_FALLBACK, self.tone(freq, db, seconds)], stderr=subprocess.DEVNULL)
        time.sleep(0.15)
        rec.send_signal(signal.SIGINT)
        rec.wait()
        return self.level(out, freq)


# ---- The window, through accessibility ----

try:
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi, GLib
except (ImportError, ValueError):  # the tests that don't touch the window can still run
    Atspi = GLib = None


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
    found, seen = [], set()

    def walk(o, depth):
        if depth > 60 or o in seen:
            return
        seen.add(o)
        try:
            if o.get_role_name() == role and (name is None or o.get_name() == name) and o.get_state_set().contains(Atspi.StateType.SHOWING):
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
        hits = wait_for(lambda: [o for o in ui_all(self.root, role, name) if ui_clickable(o)], timeout=4)
        obj = hits[0] if hits else None
        if not obj or not obj.get_action_iface().do_action(0):
            raise RuntimeError(f"couldn't click the {role} “{name}”")
        time.sleep(0.4)
        return obj

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


# ---- Delay ----


def click_delay(tmp, into, device):
    """Milliseconds from a click leaving a player to it coming out of the silent `device`.
    The player is linked both to `into` and to a recorder that also takes the device's
    monitor, so both land in the same recording whatever the start-up timing (as in
    tests/measure.py)."""
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
    rec = subprocess.Popen(["pw-record", "--target=0", "-P", "{ node.name = eqevents_rec }", "--rate", str(RATE), "--channels", "4",
                            "--channel-map", "AUX0,AUX1,AUX2,AUX3", "--format", "s16", out], stderr=subprocess.DEVNULL)
    rec_in = ports("eqevents_rec:", "-i")
    for s, d in zip(ports(f"{device}:monitor_", "-o"), rec_in[2:]):
        run("pw-link", s, d)
    # Unlinked until linked here, so it can't play anywhere else.
    play = subprocess.Popen(["pw-play", "--target=0", "-P", "{ node.name = eqevents_play node.dont-fallback = true }", clicks], stderr=subprocess.DEVNULL)
    play_out = ports("eqevents_play:", "-o")
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

    came = [abs(x) for x in v[2::4]]
    gaps = []
    for g in onsets(v[0::4])[1:]:
        window = came[g : g + every]
        if window and max(window) > 1500:
            gaps.append(window.index(max(window)))
    gaps.sort()
    # For a failure message: how loud each of the four channels got, and how many clicks.
    click_delay.last = f"{len(v)//4} frames, peaks {[max(map(abs, v[c::4]), default=0) for c in range(4)]}, {len(onsets(v[0::4]))} clicks, ports {len(rec_in)}/{len(play_out)}"
    return gaps[len(gaps) // 2] / RATE * 1000 if gaps else None


def reported_ms(sink):
    """The latency PipeWire reports for a device, as a Pulse client such as a browser sees it."""
    for block in run("pactl", "list", "sinks").split("\n\n"):
        if re.search(rf"Name: {re.escape(sink)}$", block, re.M):
            m = re.search(r"Latency: (\d+) usec", block)
            return int(m.group(1)) / 1000 if m else 0.0
    return 0.0


# ---- What the app prints ----

# Known and fine. GTK's own warnings and criticals are not: they mean a widget was used
# wrongly. Only what the test machine's own session prints belongs here, never what one
# person's settings cause.
KNOWN_APP_LINES = [r"unknown resource \d+ op:7", r"^equalizer-trace:"]


def bad_app_lines(text):
    """The lines of the app's stderr that are warnings, criticals or panics and aren't known."""
    return [l for l in text.splitlines()
            if re.search(r"WARNING|CRITICAL|panicked|\*\*\s*ERROR|Gtk-|Gdk-|Adwaita-", l) and not any(re.search(k, l) for k in KNOWN_APP_LINES)]


# ---- The "Plays through" dropdown ----

FILTER_NAMES = ("Bell", "Low", "High", "Notch")  # the combo boxes that aren't the device list


def plays_through(win):
    """The name the Output's "Plays through" dropdown shows, or None."""
    for o in ui_all(win.root, "combo box"):
        name = o.get_name() or ""
        if name and not name.startswith(FILTER_NAMES):
            return name
    return None


def choose_plays_through(win, desc, timeout=4.0):
    """Opens the dropdown and picks the entry with this text. Raises if it can't."""
    current = plays_through(win)
    if current is None:
        raise RuntimeError("couldn't find the Plays through dropdown")
    if current == desc:
        return
    win.click("toggle button", current)
    labels = wait_for(lambda: [o for o in ui_all(win.root, "label", desc) if o.get_name() == desc], timeout=timeout)
    if not labels:
        raise RuntimeError(f"the Plays through list has no “{desc}”")
    # The list is a selection: find it above the label, and select the label's row.
    o, picked = labels[-1], False
    for _ in range(6):
        parent = o.get_parent()
        if parent is None:
            break
        sel = parent.get_selection_iface()
        if sel:
            try:
                picked = bool(sel.select_child(o.get_index_in_parent()))
            except GLib.Error:
                picked = False
            break
        o = parent
    if not picked:
        # Otherwise whatever above it takes a click.
        o = labels[-1]
        for _ in range(5):
            if o is None:
                break
            if ui_clickable(o) and o.get_action_iface().do_action(0):
                picked = True
                break
            o = o.get_parent()
    if not picked:
        raise RuntimeError(f"couldn't pick “{desc}” in the Plays through list")
    if not wait_for(lambda: plays_through(win) == desc, timeout=3):
        raise RuntimeError(f"the Plays through dropdown shows “{plays_through(win)}”, not “{desc}”")
    time.sleep(0.4)
