#!/usr/bin/env python3
"""Stall detector — flags GPU-loaded but inactive in three patterns.

  STRICT: vram>min AND util<=max AND power<=strict_max AND serve_cpu>=min
          for `strict_consecutive_polls` polls in a row.
          (Classic CPU-bound hang: ollama serve burning a core, GPU idle.)

  LOOSE:  same shape but power<=loose_max AND `loose_window_match_fraction`
          of last `loose_window_polls` (sliding window).
          (CPU-bound hang with minor dips that strict misses.)

  GHOST:  vram>min AND util<=max AND request_active AND serve_cpu<ghost_max
          for `ghost_consecutive_polls` in a row.
          (Opposite signature: Ollama took the POST, model is loaded, but
          neither GPU nor CPU does anything — request hangs silently.)

Confidence ordering: strict > loose > ghost. An open lower-confidence event
is upgraded in place if a higher-confidence match occurs during the same
window. Stack capture runs once per event regardless of which criterion
opened it.
"""

import os
import signal
import subprocess
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import CONFIG, connect

CONTAINER = CONFIG["ollama"]["container_name"]
S = CONFIG["stall"]
POLL_INTERVAL = S["poll_interval_sec"]
VRAM_MIN = S["vram_min_mib"]
UTIL_MAX = S["util_max_percent"]
SERVE_CPU_MIN = S["ollama_serve_cpu_min_percent"]
STRICT_POWER_MAX = S["strict_power_max_watts"]
STRICT_NEEDED = S["strict_consecutive_polls"]
LOOSE_POWER_MAX = S["loose_power_max_watts"]
LOOSE_WINDOW = S["loose_window_polls"]
LOOSE_FRAC = S["loose_window_match_fraction"]
GHOST_CPU_MAX = S.get("ghost_serve_cpu_max_percent", 50)
GHOST_NEEDED = S.get("ghost_consecutive_polls", 6)
COOLDOWN = S["cooldown_sec"]
CAPTURE_GDB = S["capture_gdb"]
CAPTURE_PROC = S["capture_proc_stacks"]
GDB = CONFIG["gdb_path"]
SUDO = CONFIG["sudo_path"]
STALLS_DIR = CONFIG["stalls_dir"]
CLK_TCK = os.sysconf(os.sysconf_names["SC_CLK_TCK"])


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_filename():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def query_nvidia_smi():
    r = subprocess.run(
        ["nvidia-smi",
         "--query-gpu=index,memory.used,utilization.gpu,power.draw",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10
    )
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.strip().split("\n"):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4:
            continue
        out.append({"gpu_id": int(parts[0]), "vram": int(parts[1]),
                    "util": int(parts[2]), "power": float(parts[3])})
    return out


def get_serve_pid():
    # Zeitgrenze (fA-1923): ohne sie wartet der Aufruf bei einem festhaengenden
    # docker-Daemon unbegrenzt. Gemessen kostet das keine Zeilen — ohne PID
    # entsteht ohnehin keine —, aber es kostet Sichtbarkeit: der Hänger sieht im
    # Protokoll aus wie Ruhe, und `systemctl stop` wartet mit. Darum laut melden
    # und den Zyklus verwerfen. KEIN stiller ValueError-Schutz an dieser Stelle:
    # der aeussere Fehlerbehandler meldet eine unbrauchbare Ausgabe heute laut,
    # ein Schutz haette daraus Stille gemacht (beides gemessen 05.11.2026).
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Pid}}", CONTAINER],
            capture_output=True, text=True, timeout=10
        )
    except subprocess.TimeoutExpired:
        print("stall detector: docker inspect ueber 10s - Probe verworfen",
              file=sys.stderr)
        return None
    if r.returncode != 0:
        return None
    try:
        pid = int(r.stdout.strip())
        return pid if pid > 0 else None
    except ValueError:
        return None


def read_proc_stat_ticks(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        rest = data[data.rindex(")") + 2:].split()
        return int(rest[11]) + int(rest[12])
    except Exception:
        return None


def read_proc_rss_mib(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return None


def get_loaded_model():
    try:
        import urllib.request, json
        with urllib.request.urlopen("http://localhost:11434/api/ps", timeout=2) as r:
            data = json.load(r)
        models = data.get("models") or []
        if models:
            return models[0].get("name")
    except Exception:
        return None
    return None


def has_active_request():
    try:
        r = subprocess.run(
            ["docker", "logs", "--tail", "200", "--since", "60s", CONTAINER],
            capture_output=True, text=True, timeout=5
        )
        text = r.stdout + r.stderr
        completions = text.count('msg="completion request"')
        finishes = text.count('POST     "/api/generate"') + text.count('POST     "/api/chat"')
        return 1 if completions > finishes else 0
    except Exception:
        return None


def capture_stacks(serve_pid):
    STALLS_DIR.mkdir(exist_ok=True)
    fname = f"{now_filename()}_pid{serve_pid}.txt"
    path = STALLS_DIR / fname
    with open(path, "w") as out:
        out.write(f"# Stall capture {now_iso()} pid={serve_pid}\n\n")
        out.write("## Threads (/proc/<tid>/{stat,wchan,status})\n\n")
        task_dir = Path(f"/proc/{serve_pid}/task")
        if task_dir.exists():
            for tid_dir in sorted(task_dir.iterdir(), key=lambda p: int(p.name)):
                tid = tid_dir.name
                try:
                    stat = (tid_dir / "stat").read_text()
                    rest = stat[stat.rindex(")") + 2:].split()
                    state = rest[0]
                    utime = int(rest[11]); stime = int(rest[12])
                    wchan = (tid_dir / "wchan").read_text().strip() or "0"
                    out.write(f"  TID {tid:>8}  state={state}  "
                              f"cpu_ticks={utime + stime:>10}  wchan={wchan}\n")
                except Exception as e:
                    out.write(f"  TID {tid}  read-failed: {e}\n")

        if CAPTURE_PROC:
            out.write("\n## Kernel stacks (/proc/<tid>/stack)\n\n")
            try:
                tid_paths = [str(p / "stack") for p in task_dir.iterdir()]
                if tid_paths:
                    r = subprocess.run(
                        [SUDO, "head", "-50"] + tid_paths,
                        capture_output=True, text=True, timeout=10
                    )
                    out.write(r.stdout)
                    if r.stderr:
                        out.write(f"\n[stderr]\n{r.stderr}\n")
            except Exception as e:
                out.write(f"  failed: {e}\n")

        if CAPTURE_GDB and Path(GDB).exists():
            out.write("\n## gdb backtrace (Go binary, symbols stripped)\n\n")
            try:
                r = subprocess.run(
                    [SUDO, "timeout", "15", GDB, "-batch", "-nx",
                     "-ex", "set pagination off",
                     "-ex", "thread apply all bt",
                     "-p", str(serve_pid)],
                    capture_output=True, text=True, timeout=20
                )
                out.write(r.stdout)
                if r.stderr:
                    out.write(f"\n[stderr]\n{r.stderr}\n")
            except Exception as e:
                out.write(f"  failed: {e}\n")
    return path


CONFIDENCE_RANK = {"ghost": 0, "loose": 1, "strict": 2}

# Threshold for "did the GPU produce output in the recent window?"
# 10% util is well above noise-floor (5% is our stall-trigger threshold)
# but below normal decode (which sits 30-100%).
MODE_A_UTIL_THRESHOLD = 10
MODE_LOOKBACK_SEC = 60


def classify_mode(conn):
    """Look at gpu_metrics in the last MODE_LOOKBACK_SEC.
    'A' = stream-then-stall (we saw real GPU work, then it stopped)
    'B' = silent (no GPU activity at all in recent window)"""
    row = conn.execute(
        "SELECT MAX(utilization_gpu) FROM gpu_metrics "
        f"WHERE timestamp > strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-{MODE_LOOKBACK_SEC} seconds')"
    ).fetchone()
    max_util = row[0] if row and row[0] is not None else 0
    return ("A" if max_util > MODE_A_UTIL_THRESHOLD else "B"), max_util


def guess_client_ip(conn):
    """Best-effort: most recent inference request in the past 5 min that
    is not a localhost healthcheck. Single-tenant assumption: one client
    dominates at a time. If multiple distinct IPs are active, the most
    recent one wins (which is the request most likely to be the stalled
    one). NULL if no recent traffic."""
    row = conn.execute(
        "SELECT client_ip FROM ollama_requests "
        "WHERE timestamp > strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-5 minutes') "
        "  AND client_ip != '127.0.0.1' "
        "  AND endpoint IN ('/api/generate', '/api/chat') "
        "ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def close_orphan_events(conn):
    """On detector startup, any stall_event with end_ts IS NULL is from a
    previous instance that was killed mid-event. We can't tell post-hoc
    whether the hang actually ended, so we close them with a marker note.
    Prevents double-tracking the same hang across restarts (caught by
    ti-10 client correlation: parallel #88 + #89 for same hang)."""
    cur = conn.execute(
        "UPDATE stall_events SET end_ts = ?, "
        "notes = COALESCE(notes || ' | ', '') || 'auto-closed on detector restart' "
        "WHERE end_ts IS NULL",
        (now_iso(),)
    )
    if cur.rowcount > 0:
        print(f"stall_detect: closed {cur.rowcount} orphan event(s) "
              f"from previous instance(s)", file=sys.stderr)


def main():
    conn = connect()
    close_orphan_events(conn)

    strict_count = defaultdict(int)
    loose_window = defaultdict(lambda: deque(maxlen=LOOSE_WINDOW))
    ghost_count = defaultdict(int)
    open_event_id = None
    open_event_confidence = None
    last_close_t = 0.0
    prev_serve_ticks = None
    prev_t = None

    running = True
    def shutdown(signum, frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(f"stall_detect: poll={POLL_INTERVAL}s "
          f"strict=({STRICT_NEEDED}*{POLL_INTERVAL}s, power<={STRICT_POWER_MAX}W) "
          f"loose=({int(LOOSE_FRAC*100)}% of {LOOSE_WINDOW}*{POLL_INTERVAL}s, "
          f"power<={LOOSE_POWER_MAX}W) "
          f"ghost=({GHOST_NEEDED}*{POLL_INTERVAL}s, serve_cpu<{GHOST_CPU_MAX}% AND request_active) "
          f"vram>{VRAM_MIN}MiB util<={UTIL_MAX}%",
          file=sys.stderr)

    while running:
        try:
            serve_pid = get_serve_pid()
            now_t = time.monotonic()

            serve_cpu = 0.0
            if serve_pid is not None:
                ticks = read_proc_stat_ticks(serve_pid)
                if ticks is not None and prev_serve_ticks is not None and prev_t is not None:
                    dt = now_t - prev_t
                    if dt > 0:
                        serve_cpu = ((ticks - prev_serve_ticks) / CLK_TCK / dt) * 100
                prev_serve_ticks = ticks
                prev_t = now_t

            gpus = query_nvidia_smi()
            any_strict = False
            any_loose = False
            any_ghost = False
            trigger_gpu = None
            trigger_vram = None

            # request_active is expensive (docker logs subprocess) — cache per poll
            cached_active = None
            def is_active():
                nonlocal cached_active
                if cached_active is None:
                    cached_active = has_active_request()
                return bool(cached_active)

            for g in gpus:
                gid = g["gpu_id"]
                vram_loaded = g["vram"] > VRAM_MIN
                gpu_idle = g["util"] <= UTIL_MAX

                # CPU-bound paths
                cpu_hot = serve_cpu >= SERVE_CPU_MIN
                base = vram_loaded and gpu_idle and cpu_hot
                strict_match = base and g["power"] <= STRICT_POWER_MAX
                loose_match = base and g["power"] <= LOOSE_POWER_MAX

                strict_count[gid] = strict_count[gid] + 1 if strict_match else 0
                loose_window[gid].append(1 if loose_match else 0)

                if strict_count[gid] >= STRICT_NEEDED:
                    any_strict = True
                    if trigger_gpu is None:
                        trigger_gpu, trigger_vram = gid, g["vram"]
                if (len(loose_window[gid]) == LOOSE_WINDOW
                        and sum(loose_window[gid]) / LOOSE_WINDOW >= LOOSE_FRAC):
                    any_loose = True
                    if trigger_gpu is None:
                        trigger_gpu, trigger_vram = gid, g["vram"]

                # GHOST path: NOT cpu-bound but request is in flight
                ghost_match = (vram_loaded and gpu_idle
                               and serve_cpu < GHOST_CPU_MAX
                               and is_active())
                ghost_count[gid] = ghost_count[gid] + 1 if ghost_match else 0
                if ghost_count[gid] >= GHOST_NEEDED:
                    any_ghost = True
                    if trigger_gpu is None:
                        trigger_gpu, trigger_vram = gid, g["vram"]

            # Pick the highest-confidence match for this poll
            new_conf = ("strict" if any_strict else
                        "loose" if any_loose else
                        "ghost" if any_ghost else None)
            stalled_now = new_conf is not None

            # State machine: open / upgrade / close
            if stalled_now and open_event_id is None and (now_t - last_close_t) > COOLDOWN:
                rss = read_proc_rss_mib(serve_pid) if serve_pid else None
                model = get_loaded_model()
                active = 1 if (cached_active or is_active()) else 0
                path = None
                try:
                    path = capture_stacks(serve_pid) if serve_pid else None
                except Exception as e:
                    print(f"capture failed: {e}", file=sys.stderr)
                mode, max_util = classify_mode(conn)
                client_ip = guess_client_ip(conn)
                cur = conn.execute(
                    "INSERT INTO stall_events (start_ts, gpu_id, vram_used_mib, "
                    "ollama_serve_cpu, ollama_serve_rss_mib, model, stack_path, "
                    "request_active, confidence, mode, max_util_recent, client_ip) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (now_iso(), trigger_gpu, trigger_vram, round(serve_cpu, 1), rss,
                     model, str(path) if path else None, active, new_conf,
                     mode, max_util, client_ip)
                )
                open_event_id = cur.lastrowid
                open_event_confidence = new_conf
                print(f"stall_detect: STALL #{open_event_id} ({new_conf}/mode-{mode}) "
                      f"gpu={trigger_gpu} vram={trigger_vram}MiB serve_cpu={serve_cpu:.0f}% "
                      f"max_util_60s={max_util}% active={active} stack={path}",
                      file=sys.stderr)

            elif (open_event_id is not None and stalled_now
                    and CONFIDENCE_RANK[new_conf] > CONFIDENCE_RANK[open_event_confidence]):
                conn.execute(
                    "UPDATE stall_events SET confidence=? WHERE id=?",
                    (new_conf, open_event_id)
                )
                print(f"stall_detect: stall #{open_event_id} upgraded "
                      f"{open_event_confidence} -> {new_conf}", file=sys.stderr)
                open_event_confidence = new_conf

            elif not stalled_now and open_event_id is not None:
                conn.execute(
                    "UPDATE stall_events SET end_ts=? WHERE id=?",
                    (now_iso(), open_event_id)
                )
                print(f"stall_detect: stall #{open_event_id} ({open_event_confidence}) ended",
                      file=sys.stderr)
                open_event_id = None
                open_event_confidence = None
                last_close_t = now_t

        except Exception as e:
            print(f"stall_detect error: {e}", file=sys.stderr)

        time.sleep(POLL_INTERVAL)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
