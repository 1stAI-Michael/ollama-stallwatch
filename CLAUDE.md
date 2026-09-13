# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Docker-based deployment of **Open WebUI** (v0.6.43) with **Ollama** (v0.19.0) as a local LLM inference backend, plus a CPU-based embedding/reranking fallback. The setup is designed for a multi-GPU NVIDIA host with GPU passthrough.

- **Open WebUI**: Full-stack AI chat/RAG web application (Python/FastAPI backend + TypeScript/SvelteKit frontend)
- **Ollama**: Local LLM inference engine with NVIDIA CUDA support
- **tei-embed / tei-rerank**: Text Embeddings Inference on CPU (`bge-m3`, `bge-reranker-v2-m3`) — failover for GX-10
- **rerank-adapter**: Translates OpenAI `/v1/rerank` to TEI `/rerank` so the coordinator sees one format
- **Networking**: All containers communicate over a custom Docker bridge network (`openllm-net`)

## Who listens where

Read this first when something is "suddenly unreachable". Two services deliberately do **not**
answer on the practice LAN any more (fA-850, 2026-09-13) — a refused connection from a LAN host
is the intended behaviour there, not a fault. Measured 2026-09-13; re-measure with
`sudo ss -tlnp` rather than trusting this table.

| Port | Service | Bound to | LAN? | Set in |
|---|---|---|---|---|
| 11434 | ollama | `0.0.0.0` | yes | `BIND_IP` in `.env` |
| 11435 | ollama-logproxy | `127.0.0.1` + tailnet | **no** | `BIND_IP_TAILNET` in `.env` |
| 3002 | monitoring dashboard/API | tailnet only | **no** | `[api] host` in `monitoring/config.toml` |
| 3000 | openwebui | `0.0.0.0` | yes | `BIND_IP` + `OPENWEBUI_PORT` in `.env` |
| 8001 | tei-embed | `0.0.0.0` | yes | `BIND_IP` in `.env` |
| 8082 | rerank-adapter | `0.0.0.0` | yes | `BIND_IP` in `.env` |
| — | tei-rerank | container network only | no | reached through rerank-adapter |

**Symptom → cause:**

- *Connection refused on `192.168.7.130:11435` or `:3002`* — intended. Use the tailnet address
  (`100.121.5.76`). Neither has any authentication, which is why they are off the LAN.
- *`127.0.0.1:3002` refused* — also intended: `ThreadingHTTPServer` binds exactly **one**
  address, and that address is now the tailnet one. `tests/stack_check.py` follows
  `[api] host` instead of assuming localhost, so the check stays honest.
- *The whole stack refuses to start, complaining about `BIND_IP_TAILNET`* — the variable is
  missing from `.env`. That is on purpose: an empty default would silently bind `0.0.0.0`
  again and quietly undo the change. Put the tailnet address back.

**To undo** (both are one line, no rebuild):

```bash
# proxy back onto the LAN: docker-compose.yml, service ollama-logproxy
#   "127.0.0.1:11435:80"  ->  "${BIND_IP}:11435:80"
docker compose up -d --no-deps --force-recreate ollama-logproxy

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

**Context limit: Ollama truncates silently.** Prompts beyond `OLLAMA_CONTEXT_LENGTH` do not
produce an error — the server drops the front of the prompt, answers HTTP 200, and bills exactly
131072 prompt tokens. Measured 2026-08-07 across 110%/150%/300% of the slot size, 9 of 9 runs
(eval-gate P6, protocol in `/mnt/austausch/01_Agenten-Studio/eval-gate/messungen/`). Any client
that needs a hard failure must check the context itself — including tool schemas, which the
token count does not cover. The same applies one level down: both TEI containers run with
`--auto-truncate`. Telltale sign in the monitoring DB: `prompt_tokens` stuck at exactly the slot size.

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
