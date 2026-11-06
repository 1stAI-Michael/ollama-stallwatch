#!/usr/bin/env python3
"""Process metrics collector — host-side /proc reads for ollama serve and runner."""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import CONFIG, connect

CONTAINER = CONFIG["ollama"]["container_name"]
POLL_INTERVAL = CONFIG["process"]["poll_interval_sec"]
HEARTBEAT_SEC = CONFIG["process"].get("heartbeat_sec", 60)
CLK_TCK = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
PAGE_SIZE = os.sysconf(os.sysconf_names["SC_PAGE_SIZE"])


def get_serve_pid():
    """Host PID of ollama serve (= the container's PID 1)."""
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
        print("process collector: docker inspect ueber 10s - Zyklus verworfen",
              file=sys.stderr)
        return None
    if r.returncode != 0:
        return None
    pid = int(r.stdout.strip())
    return pid if pid > 0 else None


def get_runner_pids(serve_pid):
    """Children of ollama serve where comm = 'ollama'."""
    children_path = Path(f"/proc/{serve_pid}/task/{serve_pid}/children")
    if not children_path.exists():
        return []
    try:
        return [int(x) for x in children_path.read_text().split()]
    except Exception:
        return []


def read_proc_stat(pid):
    """Returns (utime+stime in clock ticks, rss in pages, num_threads). None if gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        # Field 2 (comm) is in parens and may contain spaces; split after last ')'
        rparen = data.rindex(")")
        rest = data[rparen + 2:].split()
        # Indexed from 'state' (field 3); offsets per proc(5):
        # state=0, ppid=1, pgrp=2, ... utime=11, stime=12, ... num_threads=17, ... rss=21
        utime = int(rest[11])
        stime = int(rest[12])
        num_threads = int(rest[17])
        rss = int(rest[21])
        return utime + stime, rss, num_threads
    except (FileNotFoundError, ProcessLookupError, ValueError):
        return None


def read_loadavg():
    try:
        with open("/proc/loadavg") as f:
            return float(f.read().split()[0])
    except Exception:
        return None


def read_mem_used_mib():
    try:
        with open("/proc/meminfo") as f:
            info = {}
            for line in f:
                k, _, v = line.partition(":")
                info[k.strip()] = int(v.split()[0])  # kB
        used_kb = info.get("MemTotal", 0) - info.get("MemAvailable", 0)
        return used_kb // 1024
    except Exception:
        return None


def has_changed(curr, last):
    if last is None:
        return True
    if abs(curr["cpu_percent"] - last["cpu_percent"]) > 2.0:
        return True
    if abs(curr["rss_mib"] - last["rss_mib"]) > 16:
        return True
    if curr["num_threads"] != last["num_threads"]:
        return True
    return False


def main():
    if shutil.which("docker") is None:
        print("process collector: docker not found, exiting cleanly", file=sys.stderr)
        return 0

    conn = connect()
    running = True
    prev_ticks = {}    # pid -> (ticks, wall_time)
    last_logged = {}   # role -> last record dict
    last_write_t = {}  # role -> wall time of last DB insert

    def shutdown(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(f"process collector: poll={POLL_INTERVAL}s container={CONTAINER}", file=sys.stderr)

    while running:
        try:
            serve_pid = get_serve_pid()
            if serve_pid is None:
                time.sleep(POLL_INTERVAL)
                continue

            now = time.monotonic()
            targets = [("serve", serve_pid)]
            for rpid in get_runner_pids(serve_pid):
                # Only count actual runner subprocesses (skip transient docker exec)
                try:
                    comm = Path(f"/proc/{rpid}/comm").read_text().strip()
                    if comm == "ollama":
                        targets.append(("runner", rpid))
                except Exception:
                    pass

            host_load1 = read_loadavg()
            host_mem = read_mem_used_mib()

            for role, pid in targets:
                stat = read_proc_stat(pid)
                if stat is None:
                    continue
                ticks, rss_pages, nthreads = stat
                rss_mib = rss_pages * PAGE_SIZE // (1024 * 1024)

                cpu_percent = 0.0
                key = (role, pid)
                if key in prev_ticks:
                    pticks, ptime = prev_ticks[key]
                    dt = now - ptime
                    if dt > 0:
                        cpu_percent = round(((ticks - pticks) / CLK_TCK / dt) * 100, 1)
                # Clamp negative values (PID-reuse on container restart can
                # produce a negative tick delta when a different process
                # inherits the old PID).
                if cpu_percent < 0:
                    cpu_percent = 0.0
                    prev_ticks.pop(key, None)  # reset baseline
                prev_ticks[key] = (ticks, now)

                record = {
                    "proc_role": role,
                    "host_pid": pid,
                    "cpu_percent": cpu_percent,
                    "rss_mib": rss_mib,
                    "num_threads": nthreads,
                    "host_load1": host_load1,
                    "host_mem_used_mib": host_mem,
                }

                # Force write if delta exceeds thresholds OR heartbeat elapsed
                heartbeat_due = (now - last_write_t.get(role, 0)) >= HEARTBEAT_SEC
                if has_changed(record, last_logged.get(role)) or heartbeat_due:
                    conn.execute(
                        "INSERT INTO system_metrics (proc_role, host_pid, cpu_percent, "
                        "rss_mib, num_threads, host_load1, host_mem_used_mib) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (role, pid, cpu_percent, rss_mib, nthreads,
                         host_load1, host_mem)
                    )
                    last_logged[role] = record
                    last_write_t[role] = now
        except Exception as e:
            print(f"process collector error: {e}", file=sys.stderr)

        time.sleep(POLL_INTERVAL)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
