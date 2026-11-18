#!/usr/bin/env python3
"""Records what happens around Equalizer on a real machine, for a bug report. It only
reads: it doesn't start or stop a chain, change a volume or a device, or play any sound.

While it runs it keeps one timeline of:

  - every change PipeWire reports (`pw-dump --monitor`): nodes, links and devices
    coming and going; volumes, mute and soft volumes on Equalizer's devices and
    streams, the real devices, their routes and players' streams; Bluetooth profile
    and codec; the default devices; the filter chain's volume_l / volume_r controls
  - how loud the sound is going into Equalizer's output and out of the real output's
    monitor, four times a second. The recorder's streams are marked like Equalizer's
    own meters (stream.monitor, and the Internal class on a monitor), so they don't
    turn on the microphone icon or switch Bluetooth headphones to their headset profile.
  - the delay each sink reports (pactl), every few seconds and at each mark
  - what records from a microphone: PulseAudio's source outputs, which is what
    desktops' microphone icons read
  - media players' play and pause state (MPRIS) and who sent each Play or Pause
  - the app: its saved Active switches and devices, and, with --launch, what it
    prints, including EQUALIZER_TRACE's lines (the engine's decisions)
  - the machine: PipeWire, WirePlumber, GTK and desktop versions, display scale,
    pw-top, and the PipeWire and WirePlumber config files

Press Enter at any time to add a mark: a few words, stamped into the timeline.
Ctrl-C ends it. User and host names, media titles and Bluetooth addresses are left out.
Device names, app names and process ids stay in, so look through the file before
sending it.

Usage: record.py [--launch COMMAND] [--steps FILE] [--keep MINUTES] [--out-dir DIR]

  --launch COMMAND   start Equalizer with COMMAND (e.g. "equalizer" or
                     "flatpak run io.github.zoeruda.Equalizer") with EQUALIZER_TRACE=1,
                     and keep what it prints. Without it, start the app yourself;
                     its trace is only recorded when it's started with --launch.
  --steps FILE       walk through the steps in FILE, asking after
                     each whether anything was off. One step per line: "id | text".
                     Lines starting with # are comments.
  --keep MINUTES     keep only the last MINUTES in memory, and write the report when
                     you add a mark. For leaving it running all day.
  --out-dir DIR      where the report goes (default: the home folder).

Writes ~/equalizer-record-report-<stamp>.json (saved as it goes) and
~/equalizer-record-report-<stamp>-summary.txt (the timeline, readable).
"""
import argparse, array, datetime, glob, json, math, os, platform, re, shlex, signal, subprocess, sys, threading, time

APP_ID = "io.github.zoeruda.Equalizer"
OWN = ("equalizer_sink", "equalizer_out", "equalizer_source", "equalizer_mic_in")
RATE, BLOCK = 48000, 0.25  # level readings: sample rate, seconds per reading
PRIVATE = re.compile(r"^(media\.(title|artist|album|filename|name|comment)|.*\.(user|host|hostname)|.*machine-id)$")
MAC = re.compile(r"\b[0-9A-Fa-f]{2}([:_])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}\b")
WATCHED_PARAMS = ("channelVolumes", "softVolumes", "monitorVolumes", "volume", "mute", "softMute", "monitorMute")


def run(*a, timeout=20):
    try:
        r = subprocess.run(a, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() or r.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"<{' '.join(a)} failed: {e}>"


def have(tool):
    return subprocess.run(["which", tool], capture_output=True).returncode == 0


def redact(v, keep_names=False):
    """Removes private keys from property lists, except in Equalizer's own nodes, whose
    media names say which stream is which."""
    if isinstance(v, dict):
        own = keep_names or str(v.get("node.name", "")).startswith("equalizer")
        return {k: ("<removed>" if PRIVATE.match(k) and not (own and k == "media.name") else redact(x, own)) for k, x in v.items()}
    if isinstance(v, list):
        return [redact(x, keep_names) for x in v]
    return v


def anonymize(text):
    for name, mark in [(os.environ.get("USER") or "", "<user>"), (platform.node(), "<host>")]:
        if name and len(name) > 1:
            text = re.sub(rf"\b{re.escape(name)}\b", mark, text)
    return MAC.sub(lambda m: "XX{0}XX{0}XX{0}XX{0}XX{0}XX".format(m.group(1)), text)


def clock(t):
    return datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S.%f")[:-3]


def props(o):
    return ((o.get("info") or {}).get("props")) or {}


def round_all(v):
    if isinstance(v, float):
        return round(v, 5)
    if isinstance(v, list):
        return [round_all(x) for x in v]
    return v


def vol_text(v):
    if v is None:
        return "?"
    if v <= 1e-6:
        return "0% (-inf dB)"
    return f"{100 * v ** (1 / 3):.0f}% ({20 * math.log10(v):+.1f} dB)"


class Timeline:
    """Everything that happened, with the time. With `keep`, only the last minutes."""

    def __init__(self, keep):
        self.keep = keep * 60 if keep else None
        self.items = []  # (time, kind, text, data)
        self.lock = threading.Lock()

    def add(self, kind, text, data=None, t=None):
        t = t or time.time()
        with self.lock:
            self.items.append((t, kind, text, data))
            if self.keep and len(self.items) % 200 == 0:
                cut = t - self.keep
                self.items = [i for i in self.items if i[0] >= cut or i[1] in ("mark", "step", "env")]

    def sorted(self):
        with self.lock:
            return sorted(self.items, key=lambda i: i[0])


class Graph:
    """PipeWire's objects, kept up to date from `pw-dump --monitor`, with every change
    to a watched state logged as it arrives."""

    def __init__(self, timeline):
        self.tl = timeline
        self.objects = {}
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.last = {}
        self.proc = None

    def start(self):
        self.running = True
        self.proc = subprocess.Popen(["pw-dump", "--monitor", "--no-colors"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        threading.Thread(target=self._supervise, daemon=True).start()

    def stop(self):
        self.running = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _supervise(self):
        """Reads pw-dump, and when it ends because PipeWire went away (a restart, or a
        crash), says so, forgets what it knew, and connects again once PipeWire is back."""
        while self.running:
            self._read()
            if not self.running:
                return
            self.proc.wait()
            with self.lock:
                self.objects.clear()
                self._log(time.time())
            self.tl.add("graph", "PipeWire went away: its objects are all gone until it's back")
            while self.running:
                time.sleep(0.5)
                self.proc = subprocess.Popen(["pw-dump", "--monitor", "--no-colors"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                # A pw-dump that can't connect exits at once.
                time.sleep(0.3)
                if self.proc.poll() is None:
                    self.tl.add("graph", "PipeWire is back")
                    break

    def _read(self):
        buf = []
        for line in self.proc.stdout:
            buf.append(line)
            if line.rstrip("\n") != "]":
                continue
            try:
                update = json.loads("".join(buf))
            except json.JSONDecodeError:
                buf = []
                continue
            buf = []
            t = time.time()
            with self.lock:
                for o in update:
                    self._apply(o)
                self._log(t)
            self.ready.set()

    def _apply(self, o):
        oid = o.get("id")
        if o.get("info") is None and "type" not in o:
            self.objects.pop(oid, None)
            return
        old = self.objects.get(oid)
        if old is None or "type" in o and old.get("type") != o.get("type"):
            self.objects[oid] = o
            return
        for k, v in o.items():
            if k == "info" and isinstance(v, dict) and isinstance(old.get("info"), dict):
                info = old["info"]
                for ik, iv in v.items():
                    if ik == "params" and isinstance(iv, dict):
                        info.setdefault("params", {}).update(iv)
                    else:
                        info[ik] = iv
            else:
                old[k] = v

    def nodes(self):
        return {i: o for i, o in self.objects.items() if o.get("type") == "PipeWire:Interface:Node"}

    def node(self, name):
        for i, o in self.nodes().items():
            if props(o).get("node.name") == name:
                return i, o
        return None, None

    def real_output(self):
        """The node name of the real output: what equalizer_out is linked to, and with
        the chain off, the desktop's default output."""
        with self.lock:
            out_id, _ = self.node("equalizer_out")
            nodes = self.nodes()
            if out_id is not None:
                for o in self.objects.values():
                    i = o.get("info") or {}
                    if o.get("type") == "PipeWire:Interface:Link" and i.get("output-node-id") == out_id and i.get("input-node-id") in nodes:
                        return props(nodes[i["input-node-id"]]).get("node.name")
            default = self.defaults().get("default.audio.sink")
            return default if default != "equalizer_sink" else None

    def defaults(self):
        out = {}
        for o in self.objects.values():
            if o.get("type") == "PipeWire:Interface:Metadata" and props(o).get("metadata.name") == "default":
                for m in o.get("metadata") or []:
                    if m.get("key", "").startswith("default.audio") and isinstance(m.get("value"), dict):
                        out[m["key"]] = m["value"].get("name")
        return out

    def links_from(self, nid):
        """The nodes linked to this one, in either direction."""
        out = []
        for o in self.objects.values():
            i = o.get("info") or {}
            if o.get("type") != "PipeWire:Interface:Link":
                continue
            if i.get("output-node-id") == nid:
                out.append(i.get("input-node-id"))
            elif i.get("input-node-id") == nid:
                out.append(i.get("output-node-id"))
        return out

    def key_of(self, nid, o):
        p = props(o)
        name, cls = p.get("node.name", ""), p.get("media.class", "")
        if name.startswith("equalizer") and not name.startswith("equalizer_meter"):
            return name
        if name.startswith("equalizer_meter") or name.startswith("eqrec_"):
            return None
        if cls.startswith("Audio/Sink") or cls.startswith("Audio/Source") and not cls.endswith("Virtual"):
            return f"device {name}"
        if cls in ("Stream/Output/Audio", "Stream/Input/Audio"):
            return f"stream {p.get('application.name') or name} (node {nid}, {cls.split('/')[1].lower()})"
        return None

    def compact(self):
        out = {}
        nodes = self.nodes()
        for nid, o in nodes.items():
            key = self.key_of(nid, o)
            if key is None:
                continue
            c = self.node_state(o)
            c["state"] = (o.get("info") or {}).get("state")
            if key.startswith("stream") or key.startswith("equalizer_out") or key.startswith("equalizer_mic_in"):
                c["linked to"] = sorted({str(props(nodes[i]).get("node.name")) for i in self.links_from(nid) if i in nodes})
            out[key] = c
            dev = self.objects.get(int(props(o).get("device.id", -1)))
            if key.startswith("device") and dev:
                out[key + " route"] = self.route_state(dev, props(o).get("card.profile.device"))
        for k, v in self.defaults().items():
            out[k] = v
        return out

    def node_state(self, o):
        out = {}
        params = (o.get("info") or {}).get("params") or {}
        for p in params.get("Props") or []:
            for k in WATCHED_PARAMS:
                if k in p:
                    out[k] = round_all(p[k])
            ps = p.get("params")
            if isinstance(ps, list):
                for name, value in zip(ps[0::2], ps[1::2]):
                    if isinstance(name, str) and (name.startswith("volume_") or name == "monitor.channel-volumes"):
                        out[name] = round_all(value)
        pr = props(o)
        for k in ("monitor.channel-volumes", "api.bluez5.codec", "api.bluez5.profile", "node.latency", "node.rate", "node.target", "target.object"):
            if k in pr:
                out["prop " + k] = pr[k]
        return out

    def route_state(self, dev, card_device):
        params = (dev.get("info") or {}).get("params") or {}
        routes = []
        for r in params.get("Route") or []:
            if card_device is not None and str(r.get("device")) != str(card_device):
                continue
            rp = r.get("props") or {}
            routes.append({
                "route": r.get("name"),
                "direction": r.get("direction"),
                "index": r.get("index"),
                **{k: round_all(rp[k]) for k in ("channelVolumes", "softVolumes", "volume", "mute") if k in rp},
            })
        return {"routes": routes, "profile": [p.get("name") for p in params.get("Profile") or []]}

    def _log(self, t):
        c = self.compact()
        for k in sorted(set(c) | set(self.last)):
            if c.get(k) != self.last.get(k):
                self.tl.add("graph", f"{k}: {self._describe(c.get(k), self.last.get(k))}", {"key": k, "state": c.get(k)}, t)
        self.last = c

    @staticmethod
    def _describe(new, old):
        if new is None:
            return "gone"
        if not isinstance(new, dict):
            return f"{old!r} -> {new!r}" if old is not None else repr(new)
        if old is None:
            old = {}
        parts = []
        for k in sorted(set(new) | set(old)):
            if new.get(k) != old.get(k):
                a, b = old.get(k), new.get(k)
                if k in WATCHED_PARAMS or k.startswith("volume_"):
                    f = lambda x: ("[" + ", ".join(vol_text(v) for v in x) + "]") if isinstance(x, list) and all(isinstance(v, (int, float)) for v in x) else repr(x)
                    parts.append(f"{k} {f(a)} -> {f(b)}" if a is not None else f"{k} {f(b)}")
                else:
                    parts.append(f"{k} {a!r} -> {b!r}" if a is not None else f"{k} {b!r}")
        return "new; " + "; ".join(parts) if not old else "; ".join(parts)

    def snapshot(self):
        """The full objects that matter, for the report."""
        with self.lock:
            want = set()
            for nid, o in self.nodes().items():
                if self.key_of(nid, o):
                    want.add(nid)
                    d = props(o).get("device.id")
                    if d is not None:
                        want.add(int(d))
            for o in self.objects.values():
                if o.get("type") == "PipeWire:Interface:Metadata":
                    want.add(o.get("id"))
            return redact([self.objects[i] for i in sorted(want) if i in self.objects])


class Levels:
    """How loud a node's monitor is, four times a second. It waits while the node
    doesn't exist, and starts again when it comes back."""

    def __init__(self, label, target, graph, timeline, keep):
        self.label, self.target, self.graph, self.tl = label, target, graph, timeline
        self.readings = []  # (time, peak dB, rms dB), loudest channel
        self.keep = keep * 60 if keep else None
        self.running = True
        self.proc = None
        self.errors = []

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.running = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _watch(self, proc, name, nid):
        while proc.poll() is None:
            # `target()` may take the graph's lock itself, so it's called outside it.
            moved = self.target() != name
            with self.graph.lock:
                gone = self.graph.node(name)[0] != nid
            if gone or moved or not self.running:
                proc.terminate()
                return
            time.sleep(0.25)

    def _loop(self):
        block = int(RATE * BLOCK) * 2 * 4
        while self.running:
            name = self.target()
            with self.graph.lock:
                nid, _ = self.graph.node(name) if name else (None, None)
            if nid is None:
                time.sleep(0.25)
                continue
            # Linked to the monitor only, never to a microphone, and marked as a meter:
            # it's not an app recording, and it stops rather than falling back.
            p = ("{ stream.capture.sink=true node.dont-fallback=true node.dont-reconnect=true node.passive=true "
                 f"stream.monitor=true media.class=Stream/Input/Audio/Internal node.name=eqrec_{self.label} }}")
            self.proc = subprocess.Popen(
                ["pw-record", "--raw", "--format", "f32", "--rate", str(RATE), "--channels", "2", "--target", name, "-P", p, "-"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            threading.Thread(target=self._watch, args=(self.proc, name, nid), daemon=True).start()
            while self.running:
                data = self.proc.stdout.read(block)
                if not data:
                    break
                t = time.time()
                a = array.array("f")
                a.frombytes(data[: len(data) // 8 * 8])
                peak, rms = -120.0, -120.0
                for ch in (a[0::2], a[1::2]):
                    if not ch:
                        continue
                    pk = max(abs(x) for x in ch)
                    r = math.sqrt(math.fsum(x * x for x in ch) / len(ch))
                    peak = max(peak, 20 * math.log10(pk) if pk > 1e-6 else -120.0)
                    rms = max(rms, 20 * math.log10(r) if r > 1e-6 else -120.0)
                self.readings.append((t, round(peak, 1), round(rms, 1)))
                if self.keep and len(self.readings) % 100 == 0:
                    cut = t - self.keep
                    self.readings = [r for r in self.readings if r[0] >= cut]
            err = (self.proc.stderr.read() or b"").decode(errors="replace").strip()
            if err and len(self.errors) < 20:
                self.errors.append(f"{clock(time.time())} {err}")
            self.proc.wait()
            time.sleep(0.25)


class Players:
    """Every media player's play/pause state, and every Play, Pause or Stop sent to one."""

    def __init__(self, timeline):
        self.tl = timeline
        self.states = {}
        self.running = True
        self.monitor = None
        self.callers = {}

    def start(self):
        threading.Thread(target=self._poll, daemon=True).start()
        if have("dbus-monitor"):
            self.monitor = subprocess.Popen(
                ["dbus-monitor", "--session", "--profile", "interface='org.mpris.MediaPlayer2.Player',type='method_call'"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            threading.Thread(target=self._calls, daemon=True).start()
        else:
            self.tl.add("player", "dbus-monitor isn't installed: who sends Pause isn't recorded")

    def stop(self):
        self.running = False
        if self.monitor and self.monitor.poll() is None:
            self.monitor.terminate()

    def _names(self):
        out = run("gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path", "/org/freedesktop/DBus",
                  "--method", "org.freedesktop.DBus.ListNames")
        return sorted(set(re.findall(r"'(org\.mpris\.MediaPlayer2\.[^']+)'", out)))

    def _poll(self):
        while self.running:
            seen = {}
            for n in self._names():
                out = run("gdbus", "call", "--session", "--dest", n, "--object-path", "/org/mpris/MediaPlayer2",
                          "--method", "org.freedesktop.DBus.Properties.Get", "org.mpris.MediaPlayer2.Player", "PlaybackStatus", timeout=3)
                m = re.search(r"'(Playing|Paused|Stopped)'", out)
                seen[n] = m.group(1) if m else "?"
            t = time.time()
            for n in sorted(set(seen) | set(self.states)):
                if seen.get(n) != self.states.get(n):
                    self.tl.add("player", f"player {n.removeprefix('org.mpris.MediaPlayer2.')}: {seen.get(n, 'gone')}", t=t)
            self.states = seen
            time.sleep(0.3)

    def _caller(self, sender):
        if sender not in self.callers:
            out = run("gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path", "/org/freedesktop/DBus",
                      "--method", "org.freedesktop.DBus.GetConnectionUnixProcessID", sender, timeout=3)
            m = re.search(r"uint32 (\d+)", out)
            comm = "?"
            if m:
                try:
                    comm = open(f"/proc/{m.group(1)}/comm").read().strip() + f" (pid {m.group(1)})"
                except OSError:
                    comm = f"pid {m.group(1)}"
            self.callers[sender] = comm
        return self.callers[sender]

    def _calls(self):
        for line in self.monitor.stdout:
            f = line.rstrip("\n").split("\t")
            if len(f) < 8 or f[0] != "mc":
                continue
            member, sender, dest = f[7], f[3], f[4]
            if member in ("Play", "Pause", "PlayPause", "Stop"):
                try:
                    t = float(f[1])
                except ValueError:
                    t = time.time()
                self.tl.add("player", f"{member} sent to {dest.removeprefix('org.mpris.MediaPlayer2.')} by {self._caller(sender)} ({sender})", t=t)


def source_outputs():
    """What records, as PulseAudio lists it: {index: description}. A microphone icon
    appears for each one that isn't a monitor."""
    out = run("pactl", "--format=json", "list", "source-outputs")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    sources = {}
    try:
        for s in json.loads(run("pactl", "--format=json", "list", "sources")):
            sources[s.get("index")] = s.get("name", "?")
    except json.JSONDecodeError:
        pass
    res = {}
    for o in data:
        p = o.get("properties") or {}
        res[o.get("index")] = (f"{p.get('application.name') or p.get('node.name') or '?'} "
                               f"(pid {p.get('application.process.id', '?')}, class {p.get('media.class', '?')}, "
                               f"from {sources.get(o.get('source'), o.get('source'))}"
                               f"{', corked' if o.get('corked') else ''})")
    return res


def sink_latencies():
    """{sink name: 'latency usec, configured usec'} from `pactl list sinks`."""
    out = run("pactl", "list", "sinks")
    res, name = {}, None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Name:"):
            name = line.split(":", 1)[1].strip()
        m = re.match(r"Latency: (\d+) usec, configured (\d+) usec", line)
        if m and name:
            res[name] = f"{int(m.group(1)) / 1000:.1f} ms (configured {int(m.group(2)) / 1000:.1f} ms)"
    return res


def poller(tl, stop):
    """Source outputs once a second, sink latencies every five seconds."""
    last_so, n = None, 0
    while not stop.is_set():
        so = source_outputs()
        if so is not None and so != last_so:
            old = last_so or {}
            for i in sorted(set(so) | set(old)):
                if so.get(i) != old.get(i):
                    tl.add("record", f"source output {i}: {so[i] if i in so else 'gone ('+old[i]+')'}")
            if last_so is None and not so:
                tl.add("record", "no source outputs: nothing records")
            last_so = so
        if n % 5 == 0:
            tl.add("latency", "sink latencies: " + "; ".join(f"{k} {v}" for k, v in sorted(sink_latencies().items())),
                   {"latencies": sink_latencies()})
        n += 1
        stop.wait(1.0)


def app_state():
    for base in [os.path.expanduser("~/.config"), os.path.expanduser(f"~/.var/app/{APP_ID}/config")]:
        path = os.path.join(base, "equalizer", "state.json")
        try:
            s = json.load(open(path))
        except (OSError, json.JSONDecodeError):
            continue
        return {"file": path, **{d: {k: (s.get(d) or {}).get(k) for k in ("enabled", "device", "preset")} for d in ("output", "mic")}}
    return None


def app_running():
    return "true" in run("gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path",
                         "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.NameHasOwner", APP_ID)


def config_files():
    out = {}
    for pat in ["~/.config/wireplumber/**/*", "~/.config/pipewire/**/*", "/etc/wireplumber/**/*", "/etc/pipewire/**/*"]:
        for f in sorted(glob.glob(os.path.expanduser(pat), recursive=True)):
            if os.path.isfile(f) and os.path.getsize(f) < 100_000:
                try:
                    out[f] = open(f, errors="replace").read()
                except OSError as e:
                    out[f] = f"<{e}>"
    return out


def environment():
    def first(*cmd):
        return (run(*cmd).splitlines() or ["?"])[0]
    env = {
        "os": first("sh", "-c", ". /etc/os-release && echo $PRETTY_NAME"),
        "kernel": platform.release(),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP"),
        "session": os.environ.get("XDG_SESSION_TYPE"),
        "pipewire": first("pipewire", "--version") if have("pipewire") else "?",
        "wireplumber": first("wireplumber", "--version") if have("wireplumber") else "?",
        "gtk4": first("pkg-config", "--modversion", "gtk4") if have("pkg-config") else "?",
        "libadwaita": first("pkg-config", "--modversion", "libadwaita-1") if have("pkg-config") else "?",
        "gdk_scale": os.environ.get("GDK_SCALE"),
        "display scaling (gsettings)": first("gsettings", "get", "org.gnome.desktop.interface", "text-scaling-factor") if have("gsettings") else "?",
        "kde scale (kreadconfig)": first("kreadconfig6", "--file", "kwinrc", "--group", "Xwayland", "--key", "Scale") if have("kreadconfig6") else "?",
        "lsp plugins": run("sh", "-c", "pacman -Q lsp-plugins-ladspa 2>/dev/null || rpm -q lsp-plugins-ladspa 2>/dev/null || true"),
        "app running": app_running(),
        "app state": app_state(),
        "default sink": first("pactl", "get-default-sink"),
        "default source": first("pactl", "get-default-source"),
        "pw-top": run("pw-top", "-b", "-n", "2", timeout=10) if have("pw-top") else "pw-top isn't installed",
        "config files": config_files(),
    }
    return json.loads(anonymize(json.dumps(env)))


class App:
    """Equalizer started by --launch, with what it prints kept in the timeline."""

    def __init__(self, command, timeline, log_path, keep):
        """The app gets its own session, so the recorder's Ctrl-C doesn't reach it, and
        a log file for its output, so it never loses its stderr when the recorder ends."""
        self.tl, self.keep = timeline, keep * 60 if keep else None
        self.log_path = log_path
        env = dict(os.environ, EQUALIZER_TRACE="1")
        log = open(log_path, "w")
        self.proc = subprocess.Popen(shlex.split(command), stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        log.close()
        self.lines = []  # (time, line)
        self.running = True
        threading.Thread(target=self._tail, daemon=True).start()

    def stop(self):
        self.running = False

    def _tail(self):
        with open(self.log_path) as f:
            while True:
                line = f.readline()
                if not line:
                    if self.proc.poll() is not None or not self.running:
                        break
                    time.sleep(0.1)
                    continue
                line = line.rstrip("\n")
                t = time.time()
                self.lines.append((t, anonymize(line)))
                if self.keep and len(self.lines) % 200 == 0:
                    self.lines = [x for x in self.lines if x[0] >= t - self.keep]
                self.tl.add("trace" if line.startswith("equalizer-trace:") else "app", anonymize(line), t=t)
        if self.proc.poll() is not None:
            self.tl.add("app", f"the app exited with code {self.proc.returncode}")


def read_steps(path):
    steps = []
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        sid, _, text = line.partition("|")
        steps.append((sid.strip(), text.strip()))
    return steps


def timeline_text(tl, levels, start):
    """The timeline, with a level reading every second or on a change of 3 dB."""
    lines = []
    for t, kind, text, _ in tl.sorted():
        lines.append((t, f"{clock(t)} {kind.upper():8} {text}"))
    for lv in levels:
        last = None
        for t, peak, rms in lv.readings:
            if last is None or t - last[0] >= 1.0 or abs(rms - last[2]) >= 3:
                lines.append((t, f"{clock(t)} LEVEL    {lv.label}: peak {peak:.1f} dB, rms {rms:.1f} dB"))
                last = (t, peak, rms)
    lines.sort(key=lambda x: x[0])
    return "\n".join(l for _, l in lines)


def write_report(path, env, tl, graph, levels, app, steps_done, start, final=False):
    report = {
        "format": 1,
        "started": clock(start),
        "written": clock(time.time()),
        "finished": final,
        "environment": env,
        "timeline": [{"t": round(t, 3), "time": clock(t), "kind": k, "text": x, **({"data": d} if d is not None else {})}
                     for t, k, x, d in tl.sorted()],
        "levels": {lv.label: [{"t": round(t, 3), "peak_db": p, "rms_db": r} for t, p, r in lv.readings] for lv in levels},
        "level_errors": {lv.label: lv.errors for lv in levels},
        "steps": steps_done,
        "app_output": [f"{clock(t)} {l}" for t, l in app.lines] if app else None,
        "graph_at_end": graph.snapshot(),
    }
    text = anonymize(json.dumps(report, indent=1))
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    with open(path.removesuffix(".json") + "-summary.txt", "w") as f:
        f.write(anonymize(f"Equalizer recording, started {clock(start)}\n\n{timeline_text(tl, levels, start)}\n"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--launch")
    ap.add_argument("--steps")
    ap.add_argument("--keep", type=float)
    ap.add_argument("--out-dir", default=os.path.expanduser("~"))
    args = ap.parse_args()
    for tool in ("pw-dump", "pw-record", "pactl"):
        if not have(tool):
            sys.exit(f"{tool} isn't installed")

    # Ctrl-C and a plain `kill` both end the recording cleanly. SIGINT is set again
    # because a recorder run in the background by a script inherits it as ignored.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    start = time.time()
    stamp = datetime.datetime.fromtimestamp(start).strftime("%Y%m%d-%H%M%S")
    path = os.path.join(args.out_dir, f"equalizer-record-report-{stamp}.json")
    tl = Timeline(args.keep)
    graph = Graph(tl)
    graph.start()
    if not graph.ready.wait(10):
        sys.exit("pw-dump gave nothing: is PipeWire running?")
    if args.launch and app_running():
        sys.exit("Equalizer is already running (only one copy can). Quit it, or leave out --launch.")
    app = App(args.launch, tl, path.removesuffix(".json") + "-app.log", args.keep) if args.launch else None
    if app:
        time.sleep(2)
    env = environment()
    tl.add("env", "environment recorded (see the report)")
    # Into Equalizer's output, and out of whatever real output it plays through.
    levels = [
        Levels("into-equalizer", lambda: "equalizer_sink", graph, tl, args.keep),
        Levels("out-to-device", graph.real_output, graph, tl, args.keep),
    ]
    for lv in levels:
        lv.start()
    players = Players(tl)
    players.start()
    stop = threading.Event()
    threading.Thread(target=poller, args=(tl, stop), daemon=True).start()
    steps_done = []

    marks = [0]

    def save(final=False, mark=False):
        """With --keep, the main report only holds the last minutes, so each mark gets
        a report of its own, which later saves don't overwrite."""
        target = path
        if mark and args.keep:
            marks[0] += 1
            target = path.removesuffix(".json") + f"-mark-{marks[0]}.json"
            print(f"  (the last {args.keep:g} minutes are in {target})")
        write_report(target, env, tl, graph, levels, app, steps_done, start, final)

    def snapshot_now(what):
        """What changes slowly, at a mark or the end of a step."""
        tl.add("latency", f"sink latencies {what}: " + "; ".join(f"{k} {v}" for k, v in sorted(sink_latencies().items())))
        if have("pw-top"):
            tl.add("pwtop", f"pw-top {what}:\n" + anonymize(run("pw-top", "-b", "-n", "2", timeout=10)))

    def periodic():
        while not stop.wait(10):
            save()
    threading.Thread(target=periodic, daemon=True).start()

    print(f"Recording. Report: {path}")
    if app is None and not app_running():
        print("Equalizer isn't running: start it (with EQUALIZER_TRACE=1 for its trace) or use --launch.")
    try:
        if args.steps:
            for sid, text in read_steps(args.steps):
                tl.add("step", f"step {sid}: {text}")
                print(f"\n== {sid}: {text}")
                input("   Press Enter when done: ")
                note = input("   Anything off? (Enter = no, or a few words): ").strip()
                steps_done.append({"id": sid, "time": clock(time.time()), "note": note or None})
                tl.add("step", f"step {sid} done: {'nothing off' if not note else 'OFF: ' + note}")
                snapshot_now(f"after step {sid}")
                save(mark=bool(note))
        else:
            print("Press Enter to add a mark, Ctrl-C to finish.")
            while True:
                input()
                note = input("  What did you notice? ").strip() or "mark"
                tl.add("mark", f"MARK: {note}")
                snapshot_now("at the mark")
                save(mark=True)
                print("  marked.")
    except (KeyboardInterrupt, EOFError):
        pass
    stop.set()
    for lv in levels:
        lv.stop()
    players.stop()
    graph.stop()
    if app:
        time.sleep(0.5)  # lets the last of the app's output be read
        app.stop()
    save(final=True)
    print(f"\nWrote {path}\nand {path.removesuffix('.json')}-summary.txt\nLook through them before sending: device and app names stay in.")
    if app and app.proc.poll() is None:
        print("The app started by --launch is still running.")


if __name__ == "__main__":
    main()
