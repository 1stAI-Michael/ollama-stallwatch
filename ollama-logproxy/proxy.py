#!/usr/bin/env python3
"""Logging-Proxy vor Ollama — schreibt mit, WER eine Anfrage gestellt hat.

Warum es diesen Umweg braucht (fA-338): Ollama loggt keine Header. Gemessen mit
eigenem Header, `User-Agent` und dem OpenAI-`user`-Feld gleichzeitig — null
Treffer im gesamten Server-Log. Die GIN-Zeile traegt nur Methode, Pfad, Status,
Dauer und Client-IP. Eine Zuordnung "welcher Agent hat wie viel Maschinenzeit
gekostet" ist aus den Server-Logs deshalb grundsaetzlich nicht ableitbar.

Der Proxy loest das additiv:

    Agent  ──X-Agent: agent:<agent_id>:<task>──>  :11435 (dieser Proxy)  ──>  :11434 Ollama
    Klinik ──────────────────────────────────────────────────────────────>  :11434 Ollama

Der bestehende Pfad bleibt unangetastet — wer heute direkt auf 11434 spricht,
merkt nichts. Nur wer ueber den Proxy geht, wird zugeordnet.

Nebeneffekt, der den Log-Parser schlaegt: der Proxy sieht die **Antwort** und
liest Modell und Prompt-Token direkt daraus, statt sie aus Log-Zeilen zu
korrelieren. Keine Zuordnungs-Races, keine Heuristik.

Streams werden Stueck fuer Stueck durchgereicht, nicht gesammelt. Die erste
Fassung las `r.read()` — die ganze Antwort — und schickte sie erst danach los.
Fuer `stream: true` (Normalfall bei /api/chat) macht das aus time-to-first-token
ein time-to-last-token: der Aufrufer sieht minutenlang nichts und dann alles auf
einmal. Funktional faellt das nicht auf, die Antwort ist ja vollstaendig; es
zerstoert nur genau die Eigenschaft, wegen der man streamt. Fuer die Buchhaltung
reicht das ENDE der Antwort, deshalb laeuft nur ein kleiner Mitschnitt der
letzten Bytes mit (TAIL) statt der ganzen Antwort.

Env:
    OLLAMA_URL   Upstream (default http://ollama:11434)
    PORT         Listen-Port (default 80; im Compose auf 11435 veroeffentlicht)
    DB_PATH      SQLite der Monitoring-DB (default /monitoring/monitor.db)
    AGENT_HEADER Header-Name (default X-Agent)

Nur stdlib — das Image bleibt ein schlankes python:slim, wie beim rerank-adapter.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/")
PORT = int(os.environ.get("PORT", "80"))
DB_PATH = os.environ.get("DB_PATH", "/monitoring/monitor.db")
AGENT_HEADER = os.environ.get("AGENT_HEADER", "X-Agent")

# Mitschnitt-Fenster fuer die Token-Zaehlung. Die Schluss-Zeile von Ollama
# (prompt_eval_count/eval_count) ist ein paar hundert Byte gross; 64 KiB traegt
# sie auch dann, wenn ein Stueck mitten in einer Zeile endet. Der Anfang des
# Fensters darf eine halbe Zeile sein — `zaehle` verwirft, was nicht parst.
TAIL = 65536

# Eigene Tabelle statt einer Spalte in ollama_requests: der bestehende Collector
# schreibt dort weiter unveraendert, und ein Rollback ist ein DROP TABLE.
SCHEMA = """
CREATE TABLE IF NOT EXISTS proxy_requests (
    id            INTEGER PRIMARY KEY,
    timestamp     TEXT    NOT NULL,
    agent_string  TEXT,
    agent_id      TEXT,
    task          TEXT,
    client_ip     TEXT,
    method        TEXT,
    endpoint      TEXT,
    status        INTEGER,
    duration_ms   REAL,
    model         TEXT,
    prompt_tokens INTEGER,
    eval_tokens   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_proxy_ts    ON proxy_requests(timestamp);
CREATE INDEX IF NOT EXISTS idx_proxy_agent ON proxy_requests(agent_id, timestamp);
"""


def db_init(pfad: str) -> sqlite3.Connection:
    conn = sqlite3.connect(pfad, check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def zerlege(agent: str) -> tuple[str | None, str | None]:
    """`agent:<agent_id>:<task>` -> (agent_id, task). Alles andere: unveraendert
    als agent_id, damit ein abweichendes Format nicht still verschwindet."""
    if not agent:
        return None, None
    teile = agent.split(":", 2)
    if len(teile) == 3 and teile[0] == "agent":
        return teile[1] or None, teile[2] or None
    return agent, None


def zaehle(rumpf: bytes) -> tuple[str | None, int | None, int | None]:
    """Modell + Token aus der Antwort. Deckt /api/* (prompt_eval_count) und
    /v1/* (usage.prompt_tokens) ab; bei Streams zaehlt die letzte Zeile."""
    if not rumpf:
        return None, None, None
    zeilen = [z for z in rumpf.splitlines() if z.strip()]
    for roh in reversed(zeilen[-3:]):
        text = roh.decode("utf-8", "replace").strip()
        if text.startswith("data: "):
            text = text[6:].strip()
        if text in ("", "[DONE]"):
            continue
        try:
            d = json.loads(text)
        except ValueError:
            continue
        u = d.get("usage") or {}
        p = d.get("prompt_eval_count", u.get("prompt_tokens"))
        e = d.get("eval_count", u.get("completion_tokens"))
        if d.get("model") or p is not None:
            return d.get("model"), p, e
    return None, None, None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    conn: sqlite3.Connection = None  # wird in main() gesetzt

    def log_message(self, fmt, *args):  # stdout gehoert dem Betrieb, nicht http.server
        pass

    def _durch(self, methode: str) -> None:
        laenge = int(self.headers.get("Content-Length") or 0)
        koerper = self.rfile.read(laenge) if laenge else None
        agent = self.headers.get(AGENT_HEADER) or ""
        kopf = {k: v for k, v in self.headers.items()
                if k.lower() not in ("host", "content-length", "connection")}

        req = urllib.request.Request(OLLAMA_URL + self.path, data=koerper,
                                     headers=kopf, method=methode)
        t0 = time.time()
        status, antwort = 0, b""
        try:
            with urllib.request.urlopen(req, timeout=3600) as r:
                status = r.status
                oben_laenge = r.headers.get("Content-Length")
                self.send_response(status)
                for k, v in r.headers.items():
                    if k.lower() not in ("transfer-encoding", "content-length", "connection"):
                        self.send_header(k, v)
                if oben_laenge is not None:
                    # Feste Laenge: es gibt nichts zu streamen, Ollama hat die
                    # Antwort ohnehin schon fertig.
                    antwort = r.read()
                    self.send_header("Content-Length", str(len(antwort)))
                    self.end_headers()
                    self.wfile.write(antwort)
                else:
                    # Kein Content-Length = Stream. read1() gibt zurueck, was DA
                    # ist, statt auf die volle Puffergroesse zu warten — read()
                    # wuerde hier wieder sammeln und den Stream einebnen.
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    while True:
                        stueck = r.read1(65536)
                        if not stueck:
                            break
                        self.wfile.write(b"%x\r\n" % len(stueck) + stueck + b"\r\n")
                        self.wfile.flush()
                        antwort = (antwort + stueck)[-TAIL:]
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
        except urllib.error.HTTPError as e:
            status = e.code
            antwort = e.read()
            self.send_response(status)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(antwort)))
            self.end_headers()
            self.wfile.write(antwort)
        except Exception as e:  # noqa: BLE001 — Upstream weg: ehrlich 502, nicht haengen
            status = 502
            antwort = json.dumps({"error": f"logproxy upstream: {type(e).__name__}: {e}"}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(antwort)))
            self.end_headers()
            self.wfile.write(antwort)
        finally:
            self._schreibe(agent, methode, status, (time.time() - t0) * 1000.0, antwort)

    def _schreibe(self, agent, methode, status, dauer_ms, antwort) -> None:
        # Buchhaltung darf den Datenpfad nie umwerfen: alles hier ist best effort.
        try:
            agent_id, task = zerlege(agent)
            modell, ptok, etok = zaehle(antwort)
            self.conn.execute(
                "INSERT INTO proxy_requests (timestamp, agent_string, agent_id, task, "
                "client_ip, method, endpoint, status, duration_ms, model, prompt_tokens, "
                "eval_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), agent or None,
                 agent_id, task, self.client_address[0], methode, self.path, status,
                 round(dauer_ms, 3), modell, ptok, etok))
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            print(f"logproxy: DB-Schreibfehler {type(e).__name__}: {e}", file=sys.stderr)

    def do_POST(self):  # noqa: N802
        self._durch("POST")

    def do_GET(self):  # noqa: N802
        self._durch("GET")

    def do_DELETE(self):  # noqa: N802
        self._durch("DELETE")

    def do_HEAD(self):  # noqa: N802
        self._durch("HEAD")


def main() -> int:
    Handler.conn = db_init(DB_PATH)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"logproxy: :{PORT} -> {OLLAMA_URL}, DB {DB_PATH}, Header {AGENT_HEADER}",
          file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
