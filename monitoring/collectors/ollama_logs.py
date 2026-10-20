#!/usr/bin/env python3
"""Ollama log parser — follows docker logs, extracts request metadata."""

import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import CONFIG, connect

CONTAINER = CONFIG["ollama"]["container_name"]
COMPOSE_FILE = CONFIG["ollama"]["compose_file"]

GIN_PATTERN = re.compile(
    r'\[GIN\]\s+(\d{4}/\d{2}/\d{2})\s+-\s+(\d{2}:\d{2}:\d{2})\s+\|\s+(\d+)\s+\|'
    r'\s+([^\|]+?)\s+\|\s+([^\|]+?)\s+\|\s+(\w+)\s+"([^"]+)"'
)
RUNNER_PATTERN = re.compile(
    r'msg="(?:finished setting up|context for request finished)"\s+'
    r'runner\.name=registry\.ollama\.ai/(?:library/)?([^\s]+)'
)
# Token-count source: 'loading cache slot' emits prompt=N in *tokens*
# (cache.go in Ollama). The earlier 'completion request' line also has
# prompt=N, but there it is len(req.Prompt) — bytes/chars, not tokens
# (~3-4x the token count for typical text). Use cache.go for charts.
#
# ZWEI Fassungen, weil Ollama die Zeile zwischen 0.19.0 und 0.40.x umbenannt hat
# (gemessen 20.10.2026 im Schatten-Behaelter, fA-1579 Stufe 1c):
#
#   0.19.0   msg="loading cache slot" id=0 cache=0 prompt=19 used=0 remaining=19
#   0.40.6   slot operator(): id 0 | task 0 | new prompt, n_ctx_slot = 8192,
#                             n_keep = 4, task.n_tokens = 19
#
# Der Collector waere nach einem Update NICHT gescheitert: er haette weiter Zeilen
# geschrieben, nur ohne prompt_tokens — der Token-Chart waere leer geblieben und
# niemand haette einen Fehler gesehen. Darum beide Muster, nicht eines ersetzt: die
# Produktion faehrt bis zum Umstieg 0.19.0, danach 0.40.x, und derselbe Collector
# muss beides lesen.
#
# Warum task.n_tokens und nicht "prompt eval time = ... / N tokens": task.n_tokens ist
# die GANZE Prompt-Laenge, genau wie das alte prompt=N. Die print_timing-Zeile nennt nur
# die neu berechneten Token (bei Prefix-Treffern weniger) — damit waere der Chart nach
# dem Update nicht mehr mit dem davor vergleichbar.
#
# NICHT nehmen: msg="llama-server completion request" ... prompt_len=99. Das ist auch in
# 0.40.6 die Zeichenlaenge, nicht die Tokenzahl (gemessen: prompt_len=99 bei 19 Token).
PROMPT_TOKENS_PATTERN = re.compile(
    r'msg="loading cache slot".*?prompt=(\d+)'                    # 0.19.0
    r'|new prompt,.*?task\.n_tokens\s*=\s*(\d+)'                 # 0.40.x
)


def parse_duration(dur_str):
    """Parse Go's time.Duration string (e.g. '1h2m3s', '4.999605ms', '8.005s').

    Each unit is matched with a strict suffix to avoid e.g. '999605m' being
    pulled out of '4.999605ms'. Hours/minutes/seconds are word-boundary'd
    so they don't bleed into ms/us/ns.
    """
    dur_str = dur_str.strip()
    total_ms = 0.0
    h = re.search(r'(\d+(?:\.\d+)?)h', dur_str)
    if h:
        total_ms += float(h.group(1)) * 3_600_000
    m = re.search(r'(\d+(?:\.\d+)?)m(?![sn])', dur_str)  # m not followed by s/n
    if m:
        total_ms += float(m.group(1)) * 60_000
    s = re.search(r'(\d+(?:\.\d+)?)s$', dur_str)        # s only at end of string
    if not s:
        s = re.search(r'(?<![mun\xb5])(\d+(?:\.\d+)?)s', dur_str)  # s not preceded by m/u/n/µ
    if s:
        total_ms += float(s.group(1)) * 1000
    ms = re.search(r'(\d+(?:\.\d+)?)ms', dur_str)
    if ms:
        total_ms += float(ms.group(1))
    us = re.search(r'(\d+(?:\.\d+)?)[uµ]s', dur_str)
    if us:
        total_ms += float(us.group(1)) / 1000
    ns = re.search(r'(\d+(?:\.\d+)?)ns', dur_str)
    if ns:
        total_ms += float(ns.group(1)) / 1_000_000
    return round(total_ms, 3)


def main():
    if shutil.which("docker") is None:
        print("ollama_logs: docker not found, exiting cleanly", file=sys.stderr)
        return 0

    # Skip if container does not exist (portable across hosts without Ollama)
    chk = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
        capture_output=True, text=True
    )
    if chk.returncode != 0:
        print(f"ollama_logs: container '{CONTAINER}' not present, exiting cleanly", file=sys.stderr)
        return 0

    conn = connect()
    running = True
    current_model = None
    current_prompt_tokens = None
    last_request_rowid = None

    def shutdown(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(f"ollama_logs: following {CONTAINER}", file=sys.stderr)

    proc = subprocess.Popen(
        ["docker", "compose", "-f", COMPOSE_FILE, "logs", "--follow",
         "--since", "1s", "--no-color", CONTAINER],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1
    )

    try:
        for line in proc.stdout:
            if not running:
                break

            runner_match = RUNNER_PATTERN.search(line)
            if runner_match:
                current_model = runner_match.group(1)
                if last_request_rowid:
                    conn.execute(
                        "UPDATE ollama_requests SET model=? WHERE rowid=? AND model IS NULL",
                        (current_model, last_request_rowid)
                    )
                    last_request_rowid = None
                continue

            tok_match = PROMPT_TOKENS_PATTERN.search(line)
            if tok_match:
                # Gruppe 1 = 0.19.0-Fassung, Gruppe 2 = 0.40.x-Fassung; genau eine traegt.
                treffer = tok_match.group(1) or tok_match.group(2)
                current_prompt_tokens = int(treffer)
                continue

            gin_match = GIN_PATTERN.search(line)
            if gin_match:
                date_str = gin_match.group(1).replace("/", "-")
                time_str = gin_match.group(2)
                timestamp = f"{date_str}T{time_str}Z"
                status = int(gin_match.group(3))
                duration_ms = parse_duration(gin_match.group(4))
                client_ip = gin_match.group(5).strip()
                method = gin_match.group(6)
                endpoint = gin_match.group(7)

                if endpoint in ("/", "/api/ps", "/api/version", "/api/tags"):
                    continue

                # /v1/* is the OpenAI-compatible path — that is what agentic
                # clients (Claude/Codex CLI, eval-gate) use. Without it their
                # requests land with model=NULL and stay invisible in the
                # per-model latency series. The runner/cache DEBUG lines that
                # carry model and prompt tokens are scheduler-level and fire
                # for /v1 exactly as they do for /api.
                is_inference = endpoint in ("/api/chat", "/api/generate",
                                            "/v1/chat/completions", "/v1/completions")

                # Real inference cannot complete in <50ms — even a single-token
                # decode is many ms. Sub-50ms inference calls are keep-alive /
                # probe / stream-closer calls and would otherwise consume the
                # cached prompt_tokens slot intended for the next real request.
                # Drop them entirely.
                if is_inference and duration_ms < 50:
                    continue

                model = current_model if is_inference else None
                prompt_tokens = current_prompt_tokens if is_inference else None

                cursor = conn.execute(
                    "INSERT INTO ollama_requests (timestamp, client_ip, method, endpoint, "
                    "status, duration_ms, model, prompt_tokens) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (timestamp, client_ip, method, endpoint, status, duration_ms,
                     model, prompt_tokens)
                )

                if is_inference and model is None:
                    last_request_rowid = cursor.lastrowid
                else:
                    last_request_rowid = None

                if model:
                    current_prompt_tokens = None
    except Exception as e:
        print(f"ollama_logs error: {e}", file=sys.stderr)
    finally:
        proc.terminate()
        proc.wait()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
