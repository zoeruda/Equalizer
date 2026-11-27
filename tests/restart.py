#!/usr/bin/env python3
"""The sound must come back after WirePlumber restarts with the Output on.

Starts the app on a device WirePlumber owns (it goes away and comes back with the restart,
which a pactl null sink doesn't), with two players going into Equalizer, then restarts
WirePlumber again and again. After each restart the chain's output must be linked to that
device, have an audio format, and the players' tone must arrive there. Seen on real
hardware (2026-11-26): the chain's output came back linked and running with no format,
and played silence until Active was turned off and on.

The tone is at -80 dBFS, which can't be heard. The restarts interrupt every sound on the
session: only with the user's go-ahead.

Usage: restart.py BINARY [--rounds N] [--keep-going]
  --rounds N     how many restarts (default 20)
  --keep-going   don't stop at the first silent one: restart the app and go on
  --window       show the window, so the level meters run
  --listeners    also record both monitors all along, as tests/record.py does
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from common import RATE, Tones, linked_to, node_exists, run, sinks, wait_for

ap = argparse.ArgumentParser()
ap.add_argument("binary")
ap.add_argument("--rounds", type=int, default=20)
ap.add_argument("--keep-going", action="store_true")
ap.add_argument("--window", action="store_true", help="show the window, so the level meters run")
ap.add_argument("--listeners", action="store_true", help="also record both monitors all along, as tests/record.py does")
args = ap.parse_args()
BIN = os.path.abspath(args.binary)
SINK, OUT = "equalizer_sink", "equalizer_out"
TONE_DB, FREQ = -80.0, 1000
# Anything above this at the device's monitor is the tone; silence reads about -170.
HEARD_DB = -110.0

if subprocess.run(["pgrep", "-x", "equalizer"], capture_output=True).returncode == 0:
    sys.exit("an Equalizer is already running: not touching it")
real = next((s for s in sinks() if s.startswith("alsa_output.")), None)
if not real:
    sys.exit("no output that WirePlumber owns (alsa_output.*) on this machine")

tmp = tempfile.mkdtemp(prefix="eqrestart")
cfg = os.path.join(tmp, "cfg", "equalizer")
os.makedirs(cfg)
effects = {e: {"enabled": False, "params": {}} for e in ["gate", "autogain", "compressor", "loudness", "limiter"]}
sound = {"bands": [], "preamp": 0.0, "balance": 0.0, "eq_enabled": True, "effects": effects}
json.dump(
    {
        "output": {"enabled": True, "device": real, "preset": "Flat", "sound": sound},
        "mic": {"enabled": False, "device": None, "preset": "Flat", "sound": sound},
        "run_in_background": True,
        "autostart": False,
    },
    open(os.path.join(cfg, "state.json"), "w"),
)
log_path = os.path.join(tmp, "app.log")
tones = Tones(tmp)
TONE_SECONDS, ROUND_SECONDS = 60, 20
tone = tones.tone(FREQ, TONE_DB, TONE_SECONDS)
app, players, listeners, started = None, [], [], {}


def start_app():
    global app
    env = dict(os.environ, XDG_CONFIG_HOME=os.path.join(tmp, "cfg"), EQUALIZER_TRACE="1")
    app = subprocess.Popen([BIN] + ([] if args.window else ["--background"]), env=env, stderr=open(log_path, "a"))
    if not wait_for(lambda: node_exists(SINK) and real in linked_to(OUT), timeout=15, step=0.3):
        sys.exit("Equalizer's chain didn't come up")
    time.sleep(1.5)


def stop_app():
    app.terminate()
    try:
        app.wait(timeout=15)
    except subprocess.TimeoutExpired:
        app.kill()
        return False
    return True


def keep_playing():
    """Two players on the default device, as a music player and a browser would be."""
    listeners[:] = [p for p in listeners if p.poll() is None]
    if args.listeners and not listeners:
        for target in (SINK, real):
            listeners.append(subprocess.Popen(
                ["pw-record", f"--target={target}", "-P", "{stream.capture.sink=true node.passive=true}", os.devnull],
                stderr=subprocess.DEVNULL))
    # A player that would reach the end of the tone during the next round is replaced.
    for p in players:
        if p.poll() is None and time.monotonic() - started[p] > TONE_SECONDS - ROUND_SECONDS:
            p.kill()
            p.wait()
    players[:] = [p for p in players if p.poll() is None]
    while len(players) < 2:
        latency = ["--latency=75ms"] if players else []
        players.append(subprocess.Popen(["pw-play", *latency, tone], stderr=subprocess.DEVNULL))
        started[players[-1]] = time.monotonic()


def out_format():
    """The chain's output node's own Format: empty when it has none."""
    try:
        objs = json.loads(run("pw-dump") or "[]")
    except json.JSONDecodeError:
        return None
    for o in objs:
        info = o.get("info") or {}
        if o.get("type") == "PipeWire:Interface:Node" and (info.get("props") or {}).get("node.name") == OUT:
            return info.get("params", {}).get("Format", [])
    return None


def arrives():
    out = os.path.join(tmp, "monitor.wav")
    rec = subprocess.Popen(
        ["pw-record", f"--target={real}", "-P", "{stream.capture.sink=true node.name=eqrestart_rec}",
         "--rate", str(RATE), "--channels", "2", "--format", "s16", out],
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)
    rec.send_signal(signal.SIGINT)
    rec.wait()
    return tones.level(out, FREQ)


def check():
    fmt, links, level = out_format(), linked_to(OUT), arrives()
    problems = []
    if linked_to("pw-play") != [SINK]:
        problems.append(f"the players go to {linked_to('pw-play') or 'nothing'}, not {SINK}")
    if links != [real]:
        problems.append(f"{OUT} goes to {links or 'nothing'}, not {real}")
    if not fmt:
        problems.append(f"{OUT} has no format")
    if level < HEARD_DB:
        problems.append(f"the tone doesn't arrive ({level:.0f} dB)")
    return problems, level


def about_players():
    """What became of players that aren't connected, now and a few seconds on."""
    for when in ("now", "5 s later"):
        objs = json.loads(run("pw-dump") or "[]")
        nodes = {o["id"]: o["info"] for o in objs if o.get("type") == "PipeWire:Interface:Node"}
        links = [o["info"] for o in objs if o.get("type") == "PipeWire:Interface:Link"]
        mine = []
        for i, info in nodes.items():
            if info["props"].get("node.name") == "pw-play":
                to = [(nodes.get(x["input-node-id"], {}).get("props", {}).get("node.name"), x.get("state"), x.get("error")) for x in links if x["output-node-id"] == i]
                mine.append(f"node {i} {info.get('state')} {info.get('error') or ''} links {to}")
        alive = sum(p.poll() is None for p in players)
        sinks_now = [n["props"].get("node.name") for n in nodes.values() if n["props"].get("media.class") == "Audio/Sink"]
        print(f"      players, {when}: {alive} processes alive; {mine or 'no pw-play node'}; sinks {sinks_now}")
        if when == "now":
            time.sleep(5)


def recoveries():
    """The times the app found the chain's output stuck and set it up again."""
    return open(log_path, errors="replace").read().count(f"stream: {OUT} is running without an audio format")


silent, recovered = [], []
try:
    start_app()
    keep_playing()
    time.sleep(1.5)
    problems, level = check()
    if problems:
        sys.exit("before any restart: " + "; ".join(problems))
    print(f"following {real}; before any restart the tone arrives at {level:.1f} dB")
    for n in range(1, args.rounds + 1):
        keep_playing()
        time.sleep(1.0)
        before = recoveries()
        run("systemctl", "--user", "reset-failed", "wireplumber")
        run("systemctl", "--user", "restart", "wireplumber", timeout=60)
        wait_for(lambda: node_exists(real) and real in linked_to(OUT), timeout=15, step=0.3)
        keep_playing()
        time.sleep(2.5)
        problems, level = check()
        if app.poll() is not None:
            problems.append(f"the app exited with {app.returncode}")
        again = recoveries() - before
        if again:
            recovered.append(n)
        note = f"  (found stuck and set up again, {again} suspend{'s' * (again > 1)})" if again else ""
        print(f"  restart {n:3}: " + (f"FAIL  {'; '.join(problems)}" if problems else f"ok    {level:6.1f} dB") + note, flush=True)
        if problems:
            silent.append(n)
            lines = open(log_path, errors="replace").read().splitlines()
            for line in [x for x in lines if "device:" in x or "module:" in x or "metadata:" in x or "stream:" in x][-8:]:
                print("      " + line)
            if any("players" in x for x in problems):
                about_players()
            if not args.keep_going:
                break
            stop_app()
            start_app()
finally:
    for p in players + listeners:
        p.kill()
    if app:
        stop_app()
    if silent:
        # In the home folder, unless EQUALIZER_REPORT_DIR says otherwise, as events.py does.
        keep = os.path.join(os.path.expanduser(os.environ.get("EQUALIZER_REPORT_DIR") or "~"),
                            f"equalizer-restart-app-{time.strftime('%Y%m%d-%H%M%S')}.log")
        shutil.copy(log_path, keep)
        print(f"the app's log: {keep}")
    shutil.rmtree(tmp, ignore_errors=True)

print(f"restarts that left the output stuck and were recovered by the app: {recovered or 'none'}")
print(f"FAILED: restarts {silent} of {n} left the sound broken" if silent else f"All passed ({args.rounds} restarts)")
sys.exit(1 if silent else 0)
