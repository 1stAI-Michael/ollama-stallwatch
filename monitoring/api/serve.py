#!/usr/bin/env python3
"""HTTP server: dashboard pages + read-only REST API for cross-host correlation."""

import http.server
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import CONFIG, connect

PORT = CONFIG["api"]["port"]
HOST = CONFIG["api"]["host"]
DIR = Path(__file__).parent
DASHBOARD = DIR / "dashboard.py"
HTML = DIR / "dashboard.html"
STALLS_DIR = CONFIG["stalls_dir"]


def json_response(handler, payload, status=200):
    body = json.dumps(payload, default=str).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler, text, status=200):
    body = text.encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIR), **kwargs)

    def log_message(self, fmt, *args):
        # quiet stdlib access logs; systemd already logs starts/stops
        pass

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        qs = parse_qs(url.query)

        # ===== REST =====
        if path == "/api/health":
            return json_response(self, {"ok": True, "host": CONFIG["host_id"]})

        if path == "/api/stalls":
            limit = int(qs.get("limit", ["100"])[0])
            filters = {
                "confidence": qs.get("confidence", [None])[0],   # 'strict'|'loose'|'ghost'
                "mode": qs.get("mode", [None])[0],               # 'A'|'B'
                "client_ip": qs.get("client_ip", [None])[0],     # exact IP match
            }
            at = qs.get("at", [None])[0]
            overlapping = qs.get("overlapping", [None])[0]
            if at:
                return json_response(self, self._query_stalls_overlap(
                    at, at, limit, **filters))
            if overlapping:
                try:
                    a, b = overlapping.split(",", 1)
                except ValueError:
                    return text_response(self, "overlapping must be 'start,end'\n", 400)
                return json_response(self, self._query_stalls_overlap(
                    a.strip(), b.strip(), limit, **filters))
            since = qs.get("since", ["1970-01-01T00:00:00Z"])[0]
            return json_response(self, self._query_stalls(since, limit, **filters))

        if path.startswith("/api/stalls/"):
            parts = path.split("/")
            if len(parts) == 5 and parts[4] == "stack":
                return self._serve_stack(int(parts[3]))

        if path == "/api/requests":
            return json_response(self, self._query_requests(qs))

        if path == "/api/system":
            since = qs.get("since", ["1970-01-01T00:00:00Z"])[0]
            return json_response(self, self._query_system(since))

        if path == "/api/gpu/live":
            return json_response(self, self._query_gpu_live())

        if path == "/api/gpu/series":
            since = qs.get("since", ["1970-01-01T00:00:00Z"])[0]
            gpu_id = qs.get("gpu_id", [None])[0]
            return json_response(self, self._query_gpu_series(since, gpu_id))

        # ===== HTML dashboard =====
        if path in ("/", "/dashboard.html"):
            hours = qs.get("h", ["24"])[0]
            try:
                int(hours)
            except ValueError:
                hours = "24"
            # fA-2077 (gefunden von ti11-Ops, 16.11.2026): hier stand ein nackter
            # subprocess.run ohne Blick auf den returncode. Scheiterte die Erzeugung,
            # lieferte der Datei-Handler darunter die ALTE dashboard.html mit HTTP 200
            # aus — auf ti11 27 Tage lang dieselbe Seite vom 19.10.2026.
            #
            # Warum 503 und nicht die alte Seite mit Banderole: das Dashboard ist ein
            # Diagnose-Werkzeug. Eine plausible Seite mit Daten von vorletzter Woche
            # beantwortet „laeuft es?" mit einem Ja von vorletzter Woche — genau der
            # Fall „eine Pruefung, die im Fehlerfall aussieht wie im Erfolgsfall".
            # Eine Banderole haengt davon ab, dass jemand sie liest; 27 Tage sagen,
            # dass das nicht passiert. Ein 503 kann stack_check.py pruefen.
            # Die REST-Pfade lesen SQLite direkt und sind davon nicht betroffen.
            #
            # Form wie bei /api/gpu/live weiter unten: Zeitgrenze fangen, returncode
            # pruefen, stderr sichtbar machen statt verschlucken.
            #
            # Die 15 s bleiben: gemessen 16.11.2026 braucht die Erzeugung 0,27 s (h=24)
            # bzw. 0,10 s (h=168 und h=720) — das grosse Fenster ist nach der Retention
            # sogar das billigere. 15 s sind also ~50-fache Luft, und eine haengende
            # Erzeugung soll schnell ein 503 werden, nicht spaet.
            try:
                r = subprocess.run([sys.executable, str(DASHBOARD), hours],
                                   capture_output=True, text=True, timeout=15)
            except subprocess.TimeoutExpired:
                print(f"dashboard: Erzeugung ueber 15 s (h={hours}) — 503, "
                      f"alte Seite wird NICHT ausgeliefert", file=sys.stderr, flush=True)
                return text_response(
                    self,
                    "Dashboard-Erzeugung ueberschritt 15 s.\n"
                    "Die vorhandene alte Seite wird absichtlich NICHT ausgeliefert "
                    "(fA-2077).\n", status=503)
            if r.returncode != 0:
                meldung = (r.stderr or "").strip()
                print(f"dashboard: Erzeugung gescheitert (h={hours}, exit "
                      f"{r.returncode}): {meldung or '<kein stderr>'}",
                      file=sys.stderr, flush=True)
                letzte = meldung.splitlines()[-1] if meldung else \
                    f"Exit {r.returncode} ohne Meldung"
                return text_response(
                    self,
                    f"Dashboard-Erzeugung gescheitert: {letzte}\n"
                    "Die vorhandene alte Seite wird absichtlich NICHT ausgeliefert "
                    "(fA-2077). Grund vollstaendig im Dienstprotokoll: "
                    "journalctl -u ollama-dashboard -n 30\n", status=503)
            self.path = "/dashboard.html"

        return super().do_GET()

    def _row_to_stall(self, r):
        return {"id": r[0], "start": r[1], "end": r[2], "gpu_id": r[3],
                "vram_used_mib": r[4], "ollama_serve_cpu": r[5],
                "ollama_serve_rss_mib": r[6], "model": r[7],
                "stack_url": (f"/api/stalls/{r[0]}/stack" if r[8] else None),
                "request_active": r[9], "confidence": r[10],
                "mode": r[11], "max_util_recent_pct": r[12],
                "client_ip": r[13]}

    SELECT_STALL = ("SELECT id, start_ts, end_ts, gpu_id, vram_used_mib, "
                    "ollama_serve_cpu, ollama_serve_rss_mib, model, stack_path, "
                    "request_active, confidence, mode, max_util_recent, client_ip "
                    "FROM stall_events ")

    def _stall_filters(self, qs_or_dict):
        """Build common filter clauses from confidence/mode/client_ip params."""
        clauses, params = [], []
        for field in ("confidence", "mode", "client_ip"):
            val = qs_or_dict.get(field) if isinstance(qs_or_dict, dict) else None
            if val:
                clauses.append(f"{field} = ?")
                params.append(val)
        return clauses, params

    def _query_stalls(self, since, limit, confidence=None, mode=None, client_ip=None):
        where = ["start_ts > ?"]
        params = [since]
        extra, eparams = self._stall_filters({
            "confidence": confidence, "mode": mode, "client_ip": client_ip})
        where.extend(extra); params.extend(eparams)
        params.append(limit)
        conn = connect()
        rows = conn.execute(
            self.SELECT_STALL + f"WHERE {' AND '.join(where)} "
            "ORDER BY start_ts DESC LIMIT ?", params).fetchall()
        conn.close()
        return {"host": CONFIG["host_id"],
                "items": [self._row_to_stall(r) for r in rows]}

    def _query_stalls_overlap(self, range_start, range_end, limit,
                              confidence=None, mode=None, client_ip=None):
        # An event overlaps [range_start, range_end] iff:
        #   start_ts <= range_end AND (end_ts IS NULL OR end_ts >= range_start)
        where = ["start_ts <= ?", "(end_ts IS NULL OR end_ts >= ?)"]
        params = [range_end, range_start]
        extra, eparams = self._stall_filters({
            "confidence": confidence, "mode": mode, "client_ip": client_ip})
        where.extend(extra); params.extend(eparams)
        params.append(limit)
        conn = connect()
        rows = conn.execute(
            self.SELECT_STALL + f"WHERE {' AND '.join(where)} "
            "ORDER BY start_ts DESC LIMIT ?", params).fetchall()
        conn.close()
        return {"host": CONFIG["host_id"],
                "query": {"range_start": range_start, "range_end": range_end,
                          "confidence": confidence, "mode": mode,
                          "client_ip": client_ip},
                "items": [self._row_to_stall(r) for r in rows]}

    def _serve_stack(self, stall_id):
        conn = connect()
        row = conn.execute("SELECT stack_path FROM stall_events WHERE id=?",
                           (stall_id,)).fetchone()
        conn.close()
        if not row or not row[0]:
            return text_response(self, "no stack captured for this stall\n", 404)
        path = Path(row[0])
        if not path.is_file():
            return text_response(self, f"stack file missing: {path}\n", 404)
        try:
            return text_response(self, path.read_text())
        except Exception as e:
            return text_response(self, f"read error: {e}\n", 500)

    def _query_requests(self, qs):
        since = qs.get("since", ["1970-01-01T00:00:00Z"])[0]
        until = qs.get("until", [None])[0]
        model = qs.get("model", [None])[0]
        endpoint = qs.get("endpoint", [None])[0]
        client_ip = qs.get("client_ip", [None])[0]
        min_dur = qs.get("min_duration_ms", [None])[0]
        max_dur = qs.get("max_duration_ms", [None])[0]
        status = qs.get("status", [None])[0]
        limit = int(qs.get("limit", ["500"])[0])

        where = ["timestamp > ?"]
        params = [since]
        if until:
            where.append("timestamp < ?"); params.append(until)
        if model:
            where.append("model = ?"); params.append(model)
        if endpoint:
            where.append("endpoint = ?"); params.append(endpoint)
        if client_ip:
            where.append("client_ip = ?"); params.append(client_ip)
        if min_dur:
            where.append("duration_ms >= ?"); params.append(float(min_dur))
        if max_dur:
            where.append("duration_ms <= ?"); params.append(float(max_dur))
        if status:
            where.append("status = ?"); params.append(int(status))
        params.append(limit)

        conn = connect()
        rows = conn.execute(
            "SELECT timestamp, client_ip, method, endpoint, status, duration_ms, "
            "model, prompt_tokens FROM ollama_requests "
            f"WHERE {' AND '.join(where)} ORDER BY timestamp DESC LIMIT ?",
            params
        ).fetchall()
        conn.close()
        return {"host": CONFIG["host_id"],
                "filters": {"since": since, "until": until, "model": model,
                            "endpoint": endpoint, "client_ip": client_ip,
                            "min_duration_ms": min_dur, "max_duration_ms": max_dur,
                            "status": status, "limit": limit},
                "items": [{"timestamp": r[0], "client_ip": r[1], "method": r[2],
                           "endpoint": r[3], "status": r[4], "duration_ms": r[5],
                           "model": r[6], "prompt_tokens": r[7]} for r in rows]}

    def _query_gpu_live(self):
        """Fresh nvidia-smi snapshot. Adds ~50ms latency vs DB read but always
        current — important for ti-10's 'is the GPU working RIGHT NOW' check."""
        if shutil.which("nvidia-smi") is None:
            return {"host": CONFIG["host_id"], "as_of": None,
                    "gpus": [], "error": "nvidia-smi not available"}
        try:
            r = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=index,name,memory.used,memory.total,"
                 "utilization.gpu,power.draw,temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5
            )
        except subprocess.TimeoutExpired:
            return {"host": CONFIG["host_id"], "as_of": None,
                    "gpus": [], "error": "nvidia-smi timeout"}
        if r.returncode != 0:
            return {"host": CONFIG["host_id"], "as_of": None,
                    "gpus": [], "error": r.stderr.strip()}
        gpus = []
        for line in r.stdout.strip().split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 7:
                continue
            try:
                gpus.append({
                    "gpu_id": int(parts[0]),
                    "name": parts[1],
                    "mem_used_mib": int(parts[2]),
                    "mem_total_mib": int(parts[3]),
                    "util_pct": int(parts[4]),
                    "power_w": float(parts[5]),
                    "temp_c": int(parts[6]),
                })
            except ValueError:
                continue
        return {"host": CONFIG["host_id"],
                "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "gpus": gpus}

    def _query_gpu_series(self, since, gpu_id):
        """Time series from gpu_metrics table. Note: delta-logged, so points
        may be sparse during stable periods (no change = no row)."""
        where = ["timestamp > ?"]
        params = [since]
        if gpu_id is not None:
            try:
                where.append("gpu_id = ?")
                params.append(int(gpu_id))
            except ValueError:
                return {"host": CONFIG["host_id"], "items": [],
                        "error": "gpu_id must be integer"}
        conn = connect()
        rows = conn.execute(
            "SELECT timestamp, gpu_id, gpu_name, vram_used_mib, vram_total_mib, "
            "utilization_gpu, power_draw_w, temperature FROM gpu_metrics "
            f"WHERE {' AND '.join(where)} ORDER BY timestamp", params
        ).fetchall()
        conn.close()
        return {"host": CONFIG["host_id"],
                "filters": {"since": since, "gpu_id": gpu_id},
                "note": "delta-logged: rows only when values change",
                "items": [{"ts": r[0], "gpu_id": r[1], "name": r[2],
                           "mem_used_mib": r[3], "mem_total_mib": r[4],
                           "util_pct": r[5], "power_w": r[6], "temp_c": r[7]}
                          for r in rows]}

    def _query_system(self, since):
        conn = connect()
        rows = conn.execute("""
            SELECT timestamp, proc_role, host_pid, cpu_percent, rss_mib,
                   num_threads, host_load1, host_mem_used_mib
            FROM system_metrics WHERE timestamp > ? ORDER BY timestamp
        """, (since,)).fetchall()
        conn.close()
        return {"host": CONFIG["host_id"],
                "items": [{"timestamp": r[0], "proc_role": r[1], "host_pid": r[2],
                           "cpu_percent": r[3], "rss_mib": r[4], "num_threads": r[5],
                           "host_load1": r[6], "host_mem_used_mib": r[7]} for r in rows]}


def main():
    server = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"dashboard+api on http://{HOST}:{PORT}", file=sys.stderr)
    print(f"  /              dashboard (24h, ?h=N to override)", file=sys.stderr)
    print(f"  /api/health    {{ok:true}}", file=sys.stderr)
    print(f"  /api/stalls    list stall_events (?since=, ?at=, ?overlapping=A,B, ?confidence=)", file=sys.stderr)
    print(f"  /api/stalls/<id>/stack   text stack capture", file=sys.stderr)
    print(f"  /api/requests  ollama requests (?model=&min_duration_ms=&since=&...)", file=sys.stderr)
    print(f"  /api/system    process+host metrics (?since=)", file=sys.stderr)
    print(f"  /api/gpu/live  fresh nvidia-smi snapshot (always current)", file=sys.stderr)
    print(f"  /api/gpu/series  gpu_metrics time series (?since=, ?gpu_id=)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
