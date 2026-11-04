# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Docker-based deployment of **Open WebUI** (v0.6.43) with **Ollama** (v0.40.6) as a local LLM inference backend, plus a CPU-based reranking fallback. The setup is designed for a multi-GPU NVIDIA host with GPU passthrough.

**Ollama moved from 0.19.0 to 0.40.6 on 2026-10-20** on Michael's release (fA-1579). This was not a
version bump but an **engine change**: 0.19.0 ran `gemma3` (medgemma), `qwen35`, `qwen35moe` and
`nomic-bert` on ollama's *own* Go engine, and from 0.30.0 that engine is gone — everything goes
through llama-server. 5 of 12 clinical answers changed as a result, and **no parameter brings the
old ones back**. The clinical reference set was re-created under 0.40.6; the rollback path and the
per-architecture reasons are written out above the `image:` line in `docker-compose.yml`.
Beware on the way back: `gpt-oss:20b` cannot be read by 0.40.x at all.

- **Open WebUI**: Full-stack AI chat/RAG web application (Python/FastAPI backend + TypeScript/SvelteKit frontend)
- **Ollama**: Local LLM inference engine with NVIDIA CUDA support
- **tei-rerank**: Text Embeddings Inference on CPU (`bge-reranker-v2-m3`) — failover for GX-10
- **tei-embed**: same image, `bge-m3` — **switched off 2026-10-20** (fA-1311/fA-1575: 0 requests in
  30 days, endpoint disabled since 2026-10-13). Kept behind a compose profile, not deleted:
  `docker compose --profile aus up -d tei-embed` brings it back. Port 8001 answers nothing today.
- **rerank-adapter**: Translates OpenAI `/v1/rerank` to TEI `/rerank` so the coordinator sees one format
- **Networking**: All containers communicate over a custom Docker bridge network (`openllm-net`)

## Who listens where

Read this first when something is "suddenly unreachable". **Exactly one** service deliberately
does not answer on the practice LAN: the monitoring dashboard. A refused connection to `:3002`
from a LAN host is intended behaviour, not a fault. Measured 2026-09-13; re-measure with
`sudo ss -tlnp` rather than trusting this table.

| Port | Service | Bound to | LAN? | Set in |
|---|---|---|---|---|
| 11434 | ollama | `0.0.0.0` | yes | `BIND_IP` in `.env` |
| 11435 | ollama-logproxy | `0.0.0.0` | yes | `BIND_IP` in `.env` |
| 3002 | monitoring dashboard/API | tailnet only | **no** | `[api] host` in `monitoring/config.toml` |
| 3000 | openwebui | `0.0.0.0` | yes | `BIND_IP` + `OPENWEBUI_PORT` in `.env` |
| 8001 | tei-embed | — | **off since 2026-10-20** | `profiles: ["aus"]` in `docker-compose.yml` |
| 8082 | rerank-adapter | `0.0.0.0` | yes | `BIND_IP` in `.env` |
| — | tei-rerank | container network only | no | reached through rerank-adapter |

**Where the line runs** (fA-850, Michael 2026-09-13) — the practice LAN counts as trusted, so
services are not hidden from it merely for looking exposed. What is kept off it is **disclosure,
not capability**:

- `:3002` publishes data that exists nowhere else on the network — model names, client
  addresses, a timeline — with no authentication. Closing it removes real read access, and
  nothing in the LAN ever consumed it.
- `:11435` was briefly moved off the LAN on the same day and moved back. It only proxies to
  `:11434`, which stays open and carries LAN load (ti-10, CT117). Closing the proxy prevented
  no capability whatsoever; it only looked like a security gain. Either the LAN is trusted —
  then this port may sit there too — or it is not, and then `:11434` is the piece of work,
  not this one.

Applying that rule to something new: ask whether it lets someone **read** something the network
does not otherwise expose. If yes, keep it on the tailnet. If it only re-exposes what `:11434`
already offers, leave it with the others and do not pretend otherwise.

**Symptom → cause:**

- *Connection refused on `192.168.7.130:3002`* — intended. Use the tailnet address
  (`100.121.5.76`).
- *`127.0.0.1:3002` refused* — also intended: `ThreadingHTTPServer` binds exactly **one**
  address, and that address is the tailnet one. `tests/stack_check.py` follows `[api] host`
  instead of assuming localhost, so the check does not report a healthy service as dead.

**To undo** (one line, no rebuild):

```bash
# dashboard back onto the LAN: monitoring/config.toml -> [api] host = "0.0.0.0"
sudo systemctl restart ollama-dashboard
```

## Common Commands

### Docker Operations

```bash
# All commands assume you are in the deployment root (DATA_ROOT in .env).
cd "$DATA_ROOT"                     # default: /srv/Container
docker compose up -d                # Start all services
docker compose down                 # Stop all services
docker compose logs -f openwebui    # Follow Open WebUI logs
docker compose logs -f ollama       # Follow Ollama logs
docker compose restart openwebui    # Restart Open WebUI
docker exec -it openwebui bash      # Shell into Open WebUI container
docker exec -it ollama bash         # Shell into Ollama container
```

### Ollama Model Management

```bash
docker exec ollama ollama list          # List installed models
docker exec ollama ollama pull <model>  # Download a model
docker exec ollama ollama rm <model>    # Remove a model
docker exec ollama ollama ps            # Show running models (VRAM usage)
```

### Operational Checks (`tests/`)

```bash
python3 tests/stack_check.py            # is the machine fit for the day? 24 checks, ~1 min
python3 tests/stack_check.py --schnell  # same without inference (triggers no model load)

# "Same question, same answer?" — the thing the eval-gate does NOT cover.
python3 tests/clinical_baseline.py --rauschboden   # is the set reproducible at all?
python3 tests/clinical_baseline.py --anlegen       # write the reference
python3 tests/clinical_baseline.py --vergleichen   # check against it after any change

python3 tests/tei_acceptance.py         # embeddings/reranker vs. the GX-10 reference
```

**Run `clinical_baseline.py --vergleichen` after every change to engine, model tag or context
length.** Those change answers without failing, so nothing else catches them. The prompt order is
part of the reference: the model is deterministic, but the answer depends on the slot's prefix
cache — the same prompt repeated back-to-back reproducibly yields a *different* answer than the
same prompt asked once. Do not re-run single entries in isolation.

Since `9040b8b` the script runs **one unscored pass first** (`stabilisieren()`), so the slot state
is fixed before measuring and a cold container no longer differs from a warm one. That costs
100–110 s per check run. `PROBE_OHNE_STABILISIERUNG=1` switches it off, and then a cold
`--vergleichen` **must** go red on `terminologie-1` — that is the control case. A red result there
is a real finding again; the earlier note "a red after a restart is known and harmless" no longer
applies.

**One reference per model.** `BASELINE_MODEL=<tag>` runs the same 12 prompts against another model
and writes/reads `clinical_baseline_results_<tag>.json`; without the variable everything behaves
exactly as before and uses `clinical_baseline_results.json`. The per-model filename is a **fix**,
not a feature: the variable existed before while the reference path was fixed, so switching the
model and running `--anlegen` silently overwrote the clinical reference. A foreign model can no
longer write that file — it aborts, even via `BASELINE_REFERENZ`.

Four reference sets exist, all created under 0.40.6 and each carrying its model digest:

| Model | why it has one |
|---|---|
| `alibayram/medgemma:27b` | the clinical model (2026-10-20) |
| `gemma3:27b`, `qwen3.5:27b`, `qwen3.6:35b-a3b-q4_K_M` | same engine change as medgemma, and more traffic — Michael 2026-11-03: "Ja, die drei gehören dazu" (fA-1579) |
| `gemma2:2b` | the cheap canary: loads in seconds, so an engine change can be checked in ~2 min before the big models are touched |

Only `gemma3:27b` and medgemma actually depend on the slot state — for `qwen3.5` and `qwen3.6` the
stabilizing control case stays **green**, because cold and warm answers are identical there anyway.
Their references are therefore backed by a different control: one signature bent in a copy of the
reference must make `--vergleichen` report exactly that entry.

### Inside the Open WebUI Container

```bash
# Backend (Python/FastAPI)
cd /app/backend
python -m pytest                        # Run all backend tests
python -m pytest test/apps/webui/routers/test_chats.py  # Run single test file
python -m pytest -k "test_name"         # Run test by name

# Frontend (TypeScript/SvelteKit)
cd /app
npm run dev                             # Dev server with HMR
npm run build                           # Production build
npm run test:frontend                   # Vitest unit tests
npm run lint                            # All lints (frontend + types + backend)
npm run lint:frontend                   # ESLint
npm run lint:types                      # Svelte type checking
npm run lint:backend                    # Pylint
npm run format                          # Prettier (frontend)
npm run format:backend                  # Black (backend)
```

## Architecture

### Container Layout

```
$DATA_ROOT/                          # default /srv/Container, set in .env
├── docker-compose.yml               # Service orchestration (ollama, tei-embed, tei-rerank,
│                                    #   rerank-adapter, openwebui)
├── .env                             # Bind IPs, GPU UUID, ports, project name (gitignored)
├── .env.example                     # Template for .env
├── ollama/                          # Ollama data volume (models, history) [gitignored]
├── tei-embed/                       # TEI embedding model cache [gitignored]
├── tei-rerank/                      # TEI reranker model cache [gitignored]
├── openwebui/                       # Open WebUI data volume (DB, uploads, cache, vector_db) [gitignored]
└── monitoring/                      # Lightweight monitoring + REST API (SQLite, 30d retention)
    ├── config.toml                  # Per-host config (host_id, paths, thresholds) [gitignored]
    ├── config.example.toml          # Template — install.sh creates config.toml from this
    ├── db.py                        # Schema (4 tables) + connect() helper
    ├── monitor.db                   # SQLite DB
    ├── collectors/
    │   ├── gpu.py                   # nvidia-smi delta-logger (systemd: ollama-gpu-logger)
    │   ├── ollama_logs.py           # Docker-log parser (systemd: ollama-log-parser)
    │   ├── process.py               # /proc/<pid>/stat for serve+runner (systemd: ollama-process-logger)
    │   └── stall_detect.py          # Hang detection + gdb/proc stack capture (systemd: ollama-stall-detector)
    ├── api/
    │   ├── serve.py                 # HTTP server (systemd: ollama-dashboard, port 3002)
    │   └── dashboard.py             # HTML generator with stall markers
    ├── stalls/                      # .txt stack dumps from stall_detect (30d retention)
    ├── report.py                    # CLI reporting (summary, models, gpu, clients, busy, status)
    └── install.sh                   # idempotent, host-detecting; writes systemd units + sudoers
```

### Open WebUI Internal Structure (inside container at /app)

**Backend** (`/app/backend/open_webui/`):
- `main.py` — FastAPI app entry point, mounts all routers
- `config.py` / `env.py` — Configuration and environment variable handling
- `routers/` — API endpoint handlers (~25 modules: chats, auths, models, files, knowledge, ollama, openai, retrieval, audio, images, etc.)
- `models/` — SQLAlchemy ORM models (~18 modules: users, chats, messages, files, knowledge, tools, functions, etc.)
- `socket/main.py` — Socket.IO WebSocket server for real-time updates
- `utils/middleware.py` — Request/response middleware (handles streaming proxy to LLM backends)
- `retrieval/` — RAG pipeline: document loaders, embedding models, vector store backends, web scrapers
- `utils/tools.py` — Tool/function execution engine
- `utils/mcp/` — Model Context Protocol support
- `internal/db.py` — SQLAlchemy engine setup; supports SQLite (default), PostgreSQL, MySQL
- `migrations/` — Alembic database migrations

**Frontend** (`/app/src/` — SvelteKit):
- Svelte 5 + SvelteKit with Vite
- Tailwind CSS 4, TipTap rich text editor, CodeMirror code editor
- Socket.IO client for real-time chat streaming
- i18next for localization

### Key Architectural Patterns

- **Multi-provider LLM abstraction**: Unified API layer proxying to Ollama, OpenAI, Anthropic, Google, Azure
- **RAG pipeline**: Document ingestion → chunking → Sentence-Transformers embeddings → ChromaDB vector store → BM25 + reranking
- **Plugin system**: User-uploadable Python functions/tools executed in RestrictedPython sandbox
- **Auth**: JWT sessions, OAuth2 (via Authlib), optional LDAP, role-based access control with user groups

## Configuration

Key environment variables (set in `.env` or `docker-compose.yml`):

| Variable | Current Value | Purpose |
|---|---|---|
| `OLLAMA_CONTEXT_LENGTH` | 131072 | Global context window for all Ollama models (128k = DIWA split-pool spec) |
| `OLLAMA_NUM_PARALLEL` | 2 | Requested slots per model. **Not always honoured** — see below |
| `OLLAMA_MAX_LOADED_MODELS` | 2 | Max models in VRAM simultaneously |
| `OLLAMA_MAX_QUEUE` | 16 | Requests wait instead of forcing a model unload |
| `OLLAMA_KEEP_ALIVE` | 10m | Unload idle models after 10 min |
| `OLLAMA_DEBUG` | 1 | Detailed request logging (model, tokens, timings) — the monitoring log parser depends on it |
| `OLLAMA_FLASH_ATTENTION` | 1 | Reduces KV-cache VRAM by ~30-50% |
| `OLLAMA_GPU_OVERHEAD` | 2147483648 | 2 GiB VRAM reserved for system/driver overhead |
| `OPENWEBUI_PORT` | 3000 | External port mapped to container's 8080 |
| `OLLAMA_BASE_URL` | http://ollama:11434 | Internal Docker network URL for Ollama |

**Parallel slots: measured, not configured.** `OLLAMA_NUM_PARALLEL=2` is a request, not a
guarantee. Ollama silently drops to 1 slot when the model plus its KV cache would not fit — no
error, no warning-level log, only `runner.parallel=1` at DEBUG. Measured 2026-08-09 on this host:
`qwen3.6:35b-a3b-q4_K_M` (31.4 GB) gets **1** slot, `alibayram/medgemma:27b` (16 GB) gets **2**.
Test it, do not read it: fire two concurrent requests against a warm model and compare aggregate
throughput to a single stream — identical throughput plus one request waiting the other out means
one slot. Related: the two models do not fit in VRAM together (31.4 + 16 GB > 40 GB usable), so
loading one evicts the other; a cold load costs ~92 s.

**`mistral-nemo:12b` does not load at the server default at all** (fA-1834, measured
2026-10-20, pre-existing on 0.19.0): a call without its own `num_ctx` dies with *"model
requires more system memory (45.1 GiB) than is available (35.0 GiB)"* while 21.1 GiB sat free
on the GPU — `131072 × 2 slots` as a sum. With `num_ctx=8192` it loads instantly. All 58 real
calls in two weeks were HTTP 200, so the callers set their own context; it only bites whoever
omits it. Dropping the page cache does **not** help (measured: 28 GiB free, still fails) —
ollama counting available RAM as `MemFree + SwapFree` is real but is not the cause here.

**Context limit: Ollama truncates silently — to HALF the context, not to the context.**
Prompts beyond the context produce no error: the server throws away the front of the prompt,
answers HTTP 200, and keeps roughly `n_keep + (num_ctx − n_keep)/2` tokens. Re-measured
2026-11-04 on 0.40.6 (fA-1859), with `num_ctx` sent per request:

| `num_ctx` sent | true input | `prompt_eval_count` | ratio |
|---|---|---|---|
| 2048 | 4635 | 1027 | 0.501 |
| 4096 | 4635 | 2051 | 0.501 |
| 8192 | 4635 | 4635 | fits, so nothing is cut |

Same ratio for `gemma2:2b` and `phi3:3.8b`, so it is the server (one `llama-server` for every
model since 0.40.6), not the model.

**It is an edge, not a ceiling** (corrected by TI-NAS-07-QS3 on 2026-11-04 from this very
table — the 8192 row proves it: 4635 tokens are *above* half of 8192 and were not cut):

    input ≤ num_ctx    arrives unchanged
    input > num_ctx    cut to n_keep + (num_ctx − n_keep)/2 — so you lose everything above
                       roughly HALF, not just the overhang

For `num_ctx=131072` that means 100k tokens arrive whole, while 140k tokens arrive as ≈ 65538:
74k lost, not 9k. The failure is harmless below the edge and brutal just above it. Do not read
this as "truncation starts at 65k".

The earlier note here — "bills exactly 131072 prompt tokens", measured 2026-08-07 on 0.19.0
(eval-gate P6) — no longer describes what happens either.

**There is no usable marker.** `prompt_eval_count` only helps a caller who knows the true token
count, and there is no tokenizer endpoint (`/api/tokenize` → 404 on 0.40.6). In the DEBUG log
the line `slot … new prompt, n_ctx_slot = 2048, n_keep = 4, task.n_tokens = 1027` carries the
*truncated* count, and `slot release: … truncated = 0` reads **0 even for a truncated
request** — a field with the right name that stays 0 when it happened.

**What does work: `truncate: false` in the request body** turns the silent cut into
`HTTP 400 {"error":"request (4635 tokens) exceeds the available context size (2048 tokens), try
increasing it"}`. The same request below the limit still answers 200, so the switch does not
simply always reject. The error names the **true** token count regardless of `num_ctx`, which
makes `num_ctx: 8, num_predict: 1, truncate: false` an exact tokenizer — per model, since the
same text is 4635 tokens for `gemma2:2b` and 6515 for `phi3:3.8b`.

**On the output side it is a silent context shift, not an abort.** Input 1198 tokens plus
`num_predict: 2000` at `num_ctx: 2048` returns HTTP 200 with `eval_count: 2000` — 1150 tokens
over the limit — and the log says `slot context shift, n_keep = 5, n_left = 2042,
n_discard = 1021`. The `done_reason: 'length'` comes from `num_predict`, not from the context,
so it is no marker either.

One level down the same applies: both TEI containers run with `--auto-truncate`. Telltale sign
in the monitoring DB: `prompt_tokens` stuck at exactly half the slot size.

## Monitoring

Lightweight local monitoring via Python scripts + SQLite (no Prometheus/Grafana).

### Web-Dashboard (Port 3002)

```
http://<server-ip>:3002          # 24h (default)
http://<server-ip>:3002/?h=1     # letzte Stunde
http://<server-ip>:3002/?h=24    # letzte 24 Stunden
http://<server-ip>:3002/?h=168   # letzte 7 Tage
http://<server-ip>:3002/?h=720   # letzte 30 Tage
```

Charts: GPU VRAM, GPU utilization %, temperature/power, requests/hour, model distribution, prompt tokens vs duration.
**Reachable over Tailscale only** — `<server-ip>` above means the tailnet address, not the LAN
one. The bind comes from `[api] host` in `monitoring/config.toml`, not from `BIND_IP`. See
"Who listens where" below before concluding the dashboard is down.

### CLI Reports

```bash
# Run from monitoring/ directory; commands accept an optional hours arg.
python3 monitoring/report.py summary      # Overview last 24h
python3 monitoring/report.py models       # Model usage (7d)
python3 monitoring/report.py gpu          # GPU metrics (24h)
python3 monitoring/report.py clients      # Client IPs (7d)
python3 monitoring/report.py busy         # Busiest hours (7d)
python3 monitoring/report.py status       # DB status
# Hours arg: report.py gpu 48
```

### Systemd Services

```bash
sudo systemctl status ollama-gpu-logger    # nvidia-smi alle 10s, delta-only
sudo systemctl status ollama-log-parser    # Folgt Ollama Docker-Logs
sudo systemctl status ollama-dashboard     # Web-Dashboard auf Port 3002
```

### Datenbank-Schema

**`gpu_metrics`**: timestamp, gpu_id, gpu_name, vram_used_mib, vram_total_mib, utilization_gpu, temperature, power_draw_w
**`ollama_requests`**: timestamp, client_ip, method, endpoint, status, duration_ms, model, prompt_tokens
**`system_metrics`**: timestamp, proc_role (serve/runner), host_pid, cpu_percent, rss_mib, num_threads, host_load1, host_mem_used_mib
**`stall_events`**: id, start_ts, end_ts, gpu_id, vram_used_mib, ollama_serve_cpu, ollama_serve_rss_mib, model, stack_path, request_active, confidence (strict/loose/ghost), mode (A/B), max_util_recent, client_ip, notes

### REST API (Port 3002)

- `GET /api/health` — `{ok: true, host: ...}`
- `GET /api/stalls?since=&at=&overlapping=A,B&confidence=&mode=&client_ip=&limit=` — filtered stall_events (open if `end=null`)
- `GET /api/stalls/<id>/stack` — plain-text stack dump
- `GET /api/requests?model=&since=&until=&endpoint=&client_ip=&status=&min_duration_ms=&max_duration_ms=&limit=` — filtered ollama_requests
- `GET /api/system?since=ISO` — system_metrics time series
- `GET /api/gpu/live` — fresh nvidia-smi snapshot (queries device directly, always current)
- `GET /api/gpu/series?since=&gpu_id=` — gpu_metrics time series (delta-logged: rows only when values change)

Stall confidence levels (run in parallel, highest-conf wins):
- `strict`: `vram>1GiB AND util<=5% AND power<=50W AND serve_cpu>=50%` for 6 polls (30s)
- `loose`: same shape with `power<=75W`, satisfied for 80% of last 30s sliding window
- `ghost`: `vram>1GiB AND util<=5% AND request_active AND serve_cpu<50%` for 30s (opposite signature — request accepted but nothing running anywhere)

Stall mode (orthogonal to confidence):
- `A` = stream-then-stall (GPU produced decode work in last 60s before stall — partial output may exist)
- `B` = silent (no GPU activity in last 60s — nothing to recover)

### Details

- **GPU Logger**: Pollt `nvidia-smi` alle 10s, schreibt nur bei Wertaenderung (Delta-Logging)
- **Log Parser**: Folgt `docker compose logs ollama`, extrahiert Modell, Prompt-Tokens, Dauer, Client-IP. Erkennt sowohl offizielle Modelle (`library/`) als auch Community-Modelle (z.B. `alibayram/medgemma`).
- **Process Logger**: liest `/proc/<host_pid>/{stat,status}` fuer ollama serve + runner alle 10s; berechnet CPU% als Delta ueber Intervall. Heartbeat-Write alle 60s auch ohne Delta. Negative CPU% (PID-reuse) werden auf 0 geclampt.
- **Stall Detector**: alle 5s; drei parallele Kriterien (strict/loose/ghost) — siehe REST-API-Section oben. Bei Open: gleichzeitig `mode` (A/B basierend auf MAX(util) der letzten 60s) und `client_ip` (most-recent /api/generate Request der letzten 5min) erfasst. Stack-Dump (Threads, /proc kernel-stacks via sudo, gdb backtrace) nach `monitoring/stalls/<ts>_pid<N>.txt`. Sudoers in `/etc/sudoers.d/ollama-monitor`. Bei Detector-Restart werden orphan-Events automatisch geschlossen.
- **Retention**: tiered — raw for 24 h, 5 s buckets for 7 days, 60 s buckets for 360 days.
  Run by `monitoring/retention.py` via the systemd timer `ollama-monitor-retention.timer`
  (daily 03:17). Sampled series are downsampled keeping **avg and max**; `ollama_requests`
  and `stall_events` are never downsampled, only deleted after 360 days.
  **The predecessor was a cron job that never ran once** — a multi-line command in
  `/etc/cron.d`, which the crontab format does not support. It went unnoticed for 146 days.
  Every run now writes to `maintenance_log`, and `tests/stack_check.py` fails if the last
  one is older than 25 h. Full rules in `monitoring/STANDARD.md`.
- **Container logs**: `json-file` does not rotate by default. Fleet standard is
  `max-size 25m` × `max-file 4` = 100 MB per container, set in both `/etc/docker/daemon.json`
  and `docker-compose.yml`. Takes effect only when a container is **recreated**.
  Beware: a NUL block from an unclean shutdown (2026-07-05 on ti-30 and ti11) makes
  `docker logs` stop dead at that offset while the file keeps growing — `--tail` still works,
  `--since` returns nothing. See `monitoring/STANDARD.md` §1a.
- **DB**: `monitoring/monitor.db` (SQLite WAL mode). Never compare timestamps against
  `datetime('now', …)` in SQL — the stored format uses `T`/`Z`, `datetime()` uses a space,
  and text comparison then makes every row of the current day "newer" than any cutoff of
  that day. Use `strftime('%Y-%m-%dT%H:%M:%SZ', 'now', …)`.
- **Dashboard**: Chart.js, wird bei jedem Seitenaufruf live aus SQLite generiert; rote Banderolen markieren Stall-Fenster auf GPU+CPU-Charts.
- **Portabilitaet**: `install.sh` ist idempotent + host-detecting (skipt Services wenn `nvidia-smi`/`docker` fehlen). Auf neuem Host: rsync + `config.toml` anpassen + `sudo ./install.sh`.

## Notes

- Comments in `docker-compose.yml` are bilingual (English + German).
- Image versions are pinned (not `:latest`) for controlled updates.
- The Open WebUI database (SQLite) lives at `openwebui/webui.db` under `$DATA_ROOT`.
- Vector DB data persists at `openwebui/vector_db/` under `$DATA_ROOT`.
- The monitoring stall detector requires a small NOPASSWD sudoers entry installed by `install.sh` — see README.md "Security model" for what it allows.
