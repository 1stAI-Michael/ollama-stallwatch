#!/usr/bin/env python3
"""GPU metrics collector — polls nvidia-smi, writes deltas to gpu_metrics."""

import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from db import CONFIG, connect

POLL_INTERVAL = CONFIG["gpu"]["poll_interval_sec"]
# Force a write at least this often per GPU even when nothing changed. This is
# what keeps the coarse rollup tier honest: with a 60s heartbeat every 60s
# bucket is guaranteed at least one sample, so a flat line stays visible as a
# flat line instead of as a gap. See STANDARD.md.
HEARTBEAT = CONFIG["gpu"].get("heartbeat_sec", 60)
# At or below this interval, spawning one nvidia-smi per sample is wasteful.
# Measured on ti-30: ~29 ms per call, i.e. roughly 3% of a core at 1 Hz spent
# purely on fork/exec. Below the threshold we run ONE long-lived nvidia-smi in
# --loop-ms mode and read its output instead.
STREAM_THRESHOLD_SEC = 5
# ZWEI Feldsaetze (fA-1922, gx10-01). Auf NVIDIA GB10 liefert nvidia-smi fuer
# memory.used, memory.total und power.limit "[N/A]" — gemessen 05.11.2026. Der
# urspruengliche Satz ist damit auf dieser Maschine unbrauchbar, und zwar STILL:
# parse_row faengt den ValueError und gibt None zurueck, also verwirft der
# Sammler jede Zeile. Nachgemessen: 12 s Lauf, 0 Zeilen in gpu_metrics, Unit
# dabei "active". Deshalb prueft der Sammler die Felder beim Start, statt sie
# anzunehmen, und faellt auf den schmalen Satz plus --query-compute-apps zurueck.
QUERY_FIELDS_VOLL = "index,name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw"
QUERY_FIELDS_SCHMAL = "index,name,utilization.gpu,temperature.gpu,power.draw"
QUERY_FIELDS = QUERY_FIELDS_VOLL   # wird in main() nach der Probe gesetzt
MEM_QUELLE = "query-gpu"           # oder "compute-apps-summe+MemTotal"


def meminfo_mib(schluessel):
    """Ein Wert aus /proc/meminfo in MiB."""
    with open("/proc/meminfo") as f:
        for zeile in f:
            if zeile.startswith(schluessel + ":"):
                return int(zeile.split()[1]) // 1024
    return None


def unified_speicher():
    """Belegter und gesamter Speicher auf Unified Memory.

    `--query-compute-apps=used_memory` nennt den von CUDA belegten Teil JE
    PROZESS; die Summe ist der GPU-Anteil am gemeinsamen Pool. Gesamt ist
    MemTotal, nicht eine VRAM-Groesse — es gibt hier keinen eigenen VRAM.
    """
    r = subprocess.run(
        ["nvidia-smi", "--query-compute-apps=used_memory",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10)
    summe = 0
    if r.returncode == 0:
        for zeile in r.stdout.strip().splitlines():
            zeile = zeile.strip()
            if zeile.isdigit():
                summe += int(zeile)
    return summe, meminfo_mib("MemTotal")


def felder_pruefen():
    """(Feldsatz, schmal?) — was auf DIESER Karte wirklich Zahlen gibt.

    Gibt den Wahrheitswert MIT zurueck, statt ihn draussen per `is` aus der
    Identitaet zweier Modul-Zeichenketten zu erraten (Hinweis TI-30-Ops,
    fA-1922 K10642): das traegt nur, solange genau diese Objekte zurueckkommen —
    baut spaeter jemand den Feldsatz zusammen, ist es still falsch.
    """
    r = subprocess.run(
        ["nvidia-smi", f"--query-gpu={QUERY_FIELDS_VOLL}",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10)
    if r.returncode == 0 and r.stdout.strip():
        erste = [t.strip() for t in r.stdout.strip().splitlines()[0].split(",")]
        if len(erste) == 7 and all(t.replace(".", "", 1).isdigit() for t in (erste[2], erste[3])):
            return QUERY_FIELDS_VOLL, False
    return QUERY_FIELDS_SCHMAL, True


def parse_row(line):
    parts = [p.strip() for p in line.split(",")]
    try:
        if len(parts) == 7:      # voller Satz: Speicher kommt aus query-gpu
            return {
                "gpu_id": int(parts[0]),
                "gpu_name": parts[1],
                "vram_used_mib": int(parts[2]),
                "vram_total_mib": int(parts[3]),
                "utilization_gpu": int(parts[4]),
                "temperature": int(parts[5]),
                "power_draw_w": round(float(parts[6]), 1),
            }
        if len(parts) == 5:      # schmaler Satz: Speicher wird nachgereicht
            belegt, gesamt = unified_speicher()
            return {
                "gpu_id": int(parts[0]),
                "gpu_name": parts[1],
                "vram_used_mib": belegt,
                "vram_total_mib": gesamt if gesamt is not None else 0,
                "utilization_gpu": int(parts[2]),
                "temperature": int(parts[3]),
                "power_draw_w": round(float(parts[4]), 1),
            }
    except ValueError:
        # nvidia-smi prints "[N/A]" for some fields on some cards/drivers.
        return None
    return None


def query_nvidia_smi():
    """One-shot query. Used when the poll interval is long enough to afford it."""
    result = subprocess.run(
        ["nvidia-smi", f"--query-gpu={QUERY_FIELDS}", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        return None
    rows = []
    for line in result.stdout.strip().split("\n"):
        row = parse_row(line)
        if row is not None:
            rows.append(row)
    return rows


def stream_cycles(interval_sec, is_running):
    """Yield one list-of-GPU-dicts per sampling cycle from a single nvidia-smi.

    nvidia-smi --loop-ms prints one line per GPU per cycle with the index
    restarting at 0, so a non-increasing index marks a cycle boundary.

    The child is restarted with backoff if it ever exits: a long-lived
    subprocess needs supervision, otherwise the collector would simply go quiet
    while systemd still reported the unit as active.
    """
    backoff = 1
    while is_running():
        proc = subprocess.Popen(
            ["nvidia-smi", f"--query-gpu={QUERY_FIELDS}",
             "--format=csv,noheader,nounits",
             f"--loop-ms={int(interval_sec * 1000)}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        cycle = []
        try:
            for line in proc.stdout:
                if not is_running():
                    break
                line = line.strip()
                if not line:
                    continue
                row = parse_row(line)
                if row is None:
                    continue
                if cycle and row["gpu_id"] <= cycle[-1]["gpu_id"]:
                    yield cycle
                    cycle = []
                cycle.append(row)
                backoff = 1
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if is_running():
            print(f"gpu collector: nvidia-smi stream ended, restart in {backoff}s",
                  file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def has_changed(current, last):
    if last is None:
        return True
    for key in ("vram_used_mib", "utilization_gpu", "temperature", "power_draw_w"):
        if current[key] != last[key]:
            return True
    return False


def main():
    if shutil.which("nvidia-smi") is None:
        print("gpu collector: nvidia-smi not found, exiting cleanly", file=sys.stderr)
        return 0

    conn = connect()
    last_values = {}
    last_write = {}
    running = True

    def shutdown(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    global QUERY_FIELDS, MEM_QUELLE
    QUERY_FIELDS, schmal = felder_pruefen()
    MEM_QUELLE = "compute-apps-summe+MemTotal" if schmal else "query-gpu"
    # Beim schmalen Satz kostet JEDE Probe einen zweiten nvidia-smi-Aufruf
    # (compute-apps). Der Stream-Modus spart genau diesen fork/exec — mit zwei
    # Aufrufen je Zyklus spart er nichts mehr, also bleibt es beim Einzelabruf.
    streaming = (POLL_INTERVAL <= STREAM_THRESHOLD_SEC) and not schmal
    print(f"gpu collector: poll={POLL_INTERVAL}s heartbeat={HEARTBEAT}s "
          f"mode={'stream' if streaming else 'oneshot'} "
          f"felder={'schmal+compute-apps' if schmal else 'voll'} "
          f"db={CONFIG['db_path']}", file=sys.stderr)
    if schmal:
        print("gpu collector: memory.used/.total sind [N/A] auf dieser Karte — "
              "vram_used_mib ist die Summe aus --query-compute-apps (GPU-Anteil "
              "am GEMEINSAMEN Pool), vram_total_mib ist MemTotal. mem_quelle "
              "steht in jeder Zeile.", file=sys.stderr)

    def store(rows):
        now = time.monotonic()
        for gpu in rows:
            gid = gpu["gpu_id"]
            due = now - last_write.get(gid, -1e9) >= HEARTBEAT
            if not (has_changed(gpu, last_values.get(gid)) or due):
                continue
            conn.execute(
                "INSERT INTO gpu_metrics (gpu_id, gpu_name, vram_used_mib, "
                "vram_total_mib, utilization_gpu, temperature, power_draw_w, "
                "mem_quelle) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (gid, gpu["gpu_name"], gpu["vram_used_mib"],
                 gpu["vram_total_mib"], gpu["utilization_gpu"],
                 gpu["temperature"], gpu["power_draw_w"], MEM_QUELLE)
            )
            last_values[gid] = gpu.copy()
            last_write[gid] = now

    if streaming:
        for rows in stream_cycles(POLL_INTERVAL, lambda: running):
            try:
                store(rows)
            except Exception as e:
                print(f"gpu collector error: {e}", file=sys.stderr)
    else:
        while running:
            try:
                rows = query_nvidia_smi()
                if rows:
                    store(rows)
            except Exception as e:
                print(f"gpu collector error: {e}", file=sys.stderr)
            time.sleep(POLL_INTERVAL)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
