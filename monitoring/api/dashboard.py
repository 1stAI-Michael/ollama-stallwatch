#!/usr/bin/env python3
"""HTML dashboard generator. Reads SQLite, writes dashboard.html."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import CONFIG, connect

OUT_PATH = Path(__file__).parent / "dashboard.html"

# Sane caps for the prompt-vs-duration scatter to drop garbage outliers
# (we used to have a parse_duration bug that produced 60M-second values).
MAX_DURATION_SEC = 600     # 10 min cap; longer = filtered as suspect
MAX_PROMPT_TOKENS = 300_000  # outlier filter: >300k tokens is well past any current Ollama model


def query_gpu_timeseries(conn, since):
    rows = conn.execute("""
        SELECT timestamp, gpu_id, gpu_name, vram_used_mib, vram_total_mib,
               utilization_gpu, temperature, power_draw_w
        FROM gpu_metrics WHERE timestamp > ? ORDER BY timestamp
    """, (since,)).fetchall()
    gpus = {}
    for ts, gid, name, vram_used, vram_total, util, temp, power in rows:
        if gid not in gpus:
            gpus[gid] = {"name": name, "vram_total": vram_total,
                         "ts": [], "vram": [], "util": [], "temp": [], "power": []}
        gpus[gid]["ts"].append(ts)
        gpus[gid]["vram"].append(vram_used)
        gpus[gid]["util"].append(util)
        gpus[gid]["temp"].append(temp)
        gpus[gid]["power"].append(power)
    return gpus


def query_requests_timeseries(conn, since):
    rows = conn.execute("""
        SELECT substr(timestamp, 1, 13) as hour, COUNT(*), COUNT(DISTINCT model)
        FROM ollama_requests WHERE timestamp > ?
        GROUP BY hour ORDER BY hour
    """, (since,)).fetchall()
    return {"hours": [r[0] for r in rows],
            "counts": [r[1] for r in rows],
            "models": [r[2] for r in rows]}


def query_model_distribution(conn, since):
    rows = conn.execute("""
        SELECT COALESCE(model, 'unbekannt'), COUNT(*) FROM ollama_requests
        WHERE timestamp > ? AND endpoint IN ('/api/chat', '/api/generate')
        GROUP BY model ORDER BY 2 DESC LIMIT 10
    """, (since,)).fetchall()
    return {"labels": [r[0] for r in rows], "counts": [r[1] for r in rows]}


def query_prompt_sizes(conn, since):
    rows = conn.execute("""
        SELECT timestamp, model, prompt_tokens, duration_ms
        FROM ollama_requests
        WHERE timestamp > ? AND prompt_tokens IS NOT NULL ORDER BY timestamp
    """, (since,)).fetchall()
    # Filter outliers (legacy data from parser bug had 60M-second values)
    kept = [(t, m, tok, dur) for t, m, tok, dur in rows
            if tok is not None and dur is not None
            and tok <= MAX_PROMPT_TOKENS and dur / 1000 <= MAX_DURATION_SEC]
    dropped = len(rows) - len(kept)
    return {"ts": [r[0] for r in kept], "model": [r[1] for r in kept],
            "tokens": [r[2] for r in kept], "duration": [r[3] for r in kept],
            "outliers_dropped": dropped}


def query_system_metrics(conn, since):
    rows = conn.execute("""
        SELECT timestamp, proc_role, cpu_percent, rss_mib, num_threads,
               host_load1, host_mem_used_mib
        FROM system_metrics WHERE timestamp > ? ORDER BY timestamp
    """, (since,)).fetchall()
    # Rollen NICHT fest verdrahten: hier sind es 'serve'/'runner', auf einem
    # Host ohne Ollama die Endpunktnamen (GX-10-Ops, fA-1922). Eine feste Liste
    # liess die Kurve dort leer, obwohl Zeilen da waren — ein Fehler, der wie
    # "keine Last" aussieht. Das Gegenstueck steht im JS unten: es laeuft ueber
    # die Schluessel von SYS, nicht ueber zwei Namen.
    out = {"host": {"ts": [], "load": [], "mem": []}}
    seen_host = set()
    for ts, role, cpu, rss, nthr, load1, mem in rows:
        if role != "host":
            out.setdefault(role, {"ts": [], "cpu": [], "rss": [], "threads": []})
            out[role]["ts"].append(ts)
            out[role]["cpu"].append(cpu)
            out[role]["rss"].append(rss)
            out[role]["threads"].append(nthr)
        if ts not in seen_host and load1 is not None:
            out["host"]["ts"].append(ts)
            out["host"]["load"].append(load1)
            out["host"]["mem"].append(mem)
            seen_host.add(ts)
    return out


def query_stall_events(conn, since):
    rows = conn.execute("""
        SELECT id, start_ts, end_ts, gpu_id, vram_used_mib, ollama_serve_cpu,
               ollama_serve_rss_mib, model, stack_path, request_active,
               confidence, mode
        FROM stall_events WHERE start_ts > ? ORDER BY start_ts DESC
    """, (since,)).fetchall()
    return [{"id": r[0], "start": r[1], "end": r[2], "gpu_id": r[3],
             "vram": r[4], "serve_cpu": r[5], "serve_rss": r[6],
             "model": r[7], "stack": r[8], "active": r[9],
             "confidence": r[10], "mode": r[11]} for r in rows]


def render_stall_row(s, generated):
    """One <tr> for a stall event."""
    dur = ""
    if s["start"] and s["end"]:
        try:
            a = datetime.fromisoformat(s["start"].replace("Z", "+00:00"))
            b = datetime.fromisoformat(s["end"].replace("Z", "+00:00"))
            dur = f"{int((b - a).total_seconds())}s"
        except Exception:
            pass
    elif s["start"] and not s["end"]:
        dur = "OPEN"
    stack = (f'<a href="/api/stalls/{s["id"]}/stack" style="color:#60a5fa">view</a>'
             if s["stack"] else "—")
    conf = s["confidence"] or "—"
    mode = s["mode"] or "—"
    return (f'<tr><td><span class="badge-stall">#{s["id"]}</span></td>'
            f'<td>{s["start"]}</td><td>{s["end"] or "—"}</td><td>{dur}</td>'
            f'<td>{conf}/{mode}</td>'
            f'<td>{s["gpu_id"]}</td><td>{s["vram"]} MiB</td>'
            f'<td>{(s["serve_cpu"] or 0):.0f}%</td>'
            f'<td>{s["model"] or "—"}</td><td>{stack}</td></tr>')


STALL_TABLE_HEAD = (
    '<table class="stalls"><tr><th>ID</th><th>Start (UTC)</th><th>Ende</th>'
    '<th>Dauer</th><th>conf/mode</th><th>GPU</th><th>VRAM</th>'
    '<th>serve CPU</th><th>Model</th><th>Stack</th></tr>'
)


def generate_html(hours):
    if not CONFIG["db_path"].exists():
        print(f"db not found: {CONFIG['db_path']}", file=sys.stderr)
        sys.exit(1)
    conn = connect()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    gpu_data = query_gpu_timeseries(conn, since)
    req_data = query_requests_timeseries(conn, since)
    model_data = query_model_distribution(conn, since)
    prompt_data = query_prompt_sizes(conn, since)
    sys_data = query_system_metrics(conn, since)
    stalls = query_stall_events(conn, since)
    conn.close()

    label = f"{hours}h" if hours < 48 else f"{hours // 24}d"
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    host = CONFIG["host_id"]

    # Stall annotations for chart.js
    stall_bands = []
    for s in stalls:
        if s["start"]:
            stall_bands.append({
                "start": s["start"],
                "end": s["end"] or generated.replace(" ", "T") + "Z",
                "label": (f"#{s['id']} {s['model'] or '?'} "
                          f"({(s['serve_cpu'] or 0):.0f}% CPU)")
            })

    # Stall section: first 3 visible, rest collapsible
    stall_html = ""
    if not stalls:
        stall_html = '<p class="no-stalls">Keine Stalls im Zeitraum.</p>'
    else:
        head_3 = stalls[:3]
        tail = stalls[3:]
        stall_html = STALL_TABLE_HEAD
        for s in head_3:
            stall_html += render_stall_row(s, generated)
        stall_html += "</table>"
        if tail:
            stall_html += (f'<details style="margin-top:8px;">'
                           f'<summary style="cursor:pointer; color:#888; font-size:0.85em; '
                           f'padding:6px 0;">▸ {len(tail)} weitere Stall-Events</summary>'
                           f'{STALL_TABLE_HEAD}')
            for s in tail:
                stall_html += render_stall_row(s, generated)
            stall_html += "</table></details>"

    # Time-range nav with active highlight
    ranges = [(1, "1h"), (6, "6h"), (24, "24h"), (168, "7d"), (720, "30d")]
    nav_html = ""
    for h, lbl in ranges:
        cls = ' class="active"' if h == hours else ""
        nav_html += f'<a href="/?h={h}"{cls}>{lbl}</a> '

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ollama Monitor — {host} ({label})</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0f1117; color: #e0e0e0; padding: 16px; }}
h1 {{ font-size: 1.3em; margin-bottom: 4px; color: #fff; }}
.meta {{ font-size: 0.85em; color: #888; margin-bottom: 16px; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
.card {{ background: #1a1d27; border-radius: 8px; padding: 16px; }}
.card h2 {{ font-size: 1em; color: #aaa; margin-bottom: 8px; }}
.full {{ grid-column: 1 / -1; }}
canvas {{ max-height: 280px; }}
.nav {{ margin-bottom: 16px; display: flex; gap: 8px; }}
.nav a {{ color: #60a5fa; text-decoration: none; padding: 4px 12px;
          border: 1px solid #333; border-radius: 4px; font-size: 0.9em; }}
.nav a:hover {{ background: #1e293b; }}
.nav a.active {{ background: #1e40af; color: #fff; border-color: #1e40af; }}
table.stalls {{ width: 100%; border-collapse: collapse; font-size: 0.85em; }}
table.stalls th, table.stalls td {{ padding: 6px 10px; text-align: left;
                                    border-bottom: 1px solid #2a2d3a; }}
table.stalls th {{ color: #888; font-weight: normal; }}
.badge-stall {{ background: #dc2626; color: white; padding: 2px 8px;
                border-radius: 4px; font-size: 0.85em; }}
.no-stalls {{ color: #4ade80; }}
details summary:hover {{ color: #60a5fa; }}
.note {{ color: #666; font-size: 0.78em; margin-top: 4px; }}
@media (max-width: 768px) {{ .grid {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<h1>Ollama Monitor — {host}</h1>
<p class="meta">Zeitraum: {label} | Erzeugt: {generated} | <a href="/api/stalls" style="color:#60a5fa">/api/stalls</a> · <a href="/api/requests" style="color:#60a5fa">/api/requests</a></p>
<div class="nav">{nav_html}</div>

<div class="card full" style="margin-bottom:16px;">
  <h2>Stall-Events (Hang-Verdacht: VRAM belegt + GPU 0% + serve CPU hoch)</h2>
  {stall_html}
</div>

<div class="grid">
  <div class="card full"><h2>GPU VRAM (MiB)</h2><canvas id="vramChart"></canvas></div>
  <div class="card full"><h2>GPU Auslastung %</h2><canvas id="utilChart"></canvas></div>
  <div class="card full"><h2>GPU Temp / Power</h2><canvas id="tempChart"></canvas></div>
  <div class="card full"><h2>Ollama serve — CPU% / RSS (MiB)</h2><canvas id="sysChart"></canvas></div>
  <div class="card"><h2>Requests pro Stunde</h2><canvas id="reqChart"></canvas></div>
  <div class="card"><h2>Modell-Verteilung</h2><canvas id="modelChart"></canvas></div>
  <div class="card full">
    <h2>Prompt-Tokens vs Dauer</h2>
    <canvas id="promptChart"></canvas>
    {('<p class="note">' + str(prompt_data["outliers_dropped"]) + ' Outlier ausgefiltert (Dauer>' + str(MAX_DURATION_SEC) + 's oder Prompt>' + str(MAX_PROMPT_TOKENS) + ').</p>') if prompt_data["outliers_dropped"] else ''}
  </div>
</div>

<script>
const COLORS = ['#60a5fa', '#f472b6', '#34d399', '#fbbf24', '#a78bfa', '#fb923c', '#22d3ee', '#facc15'];
const GPU = {json.dumps(gpu_data, default=str)};
const REQ = {json.dumps(req_data)};
const MODEL = {json.dumps(model_data)};
const PROMPT = {json.dumps(prompt_data)};
const SYS = {json.dumps(sys_data)};
const STALLS = {json.dumps(stall_bands)};

// Build a stable model -> color map (shared by Modell-Verteilung and Prompt scatter)
const MODEL_COLOR = {{}};
MODEL.labels.forEach((m, i) => {{ MODEL_COLOR[m] = COLORS[i % COLORS.length]; }});
function colorForModel(m) {{
  if (!MODEL_COLOR[m]) {{
    MODEL_COLOR[m] = COLORS[Object.keys(MODEL_COLOR).length % COLORS.length];
  }}
  return MODEL_COLOR[m];
}}

Chart.defaults.color = '#888';
Chart.defaults.borderColor = '#2a2d3a';
Chart.defaults.font.size = 11;

function timeAxis() {{
  return {{ type: 'time',
           time: {{ tooltipFormat: 'dd.MM HH:mm:ss',
                    displayFormats: {{ hour: 'HH:mm', day: 'dd.MM' }} }},
           ticks: {{ maxTicksLimit: 12 }} }};
}}

const stallPlugin = {{
  id: 'stallBands',
  beforeDraw(chart) {{
    if (!STALLS.length) return;
    const {{ ctx, chartArea, scales: {{ x }} }} = chart;
    if (!x) return;
    ctx.save();
    ctx.fillStyle = 'rgba(220, 38, 38, 0.15)';
    STALLS.forEach(b => {{
      const x0 = x.getPixelForValue(b.start);
      const x1 = x.getPixelForValue(b.end);
      if (x0 < chartArea.right && x1 > chartArea.left) {{
        ctx.fillRect(Math.max(x0, chartArea.left), chartArea.top,
                     Math.min(x1, chartArea.right) - Math.max(x0, chartArea.left),
                     chartArea.bottom - chartArea.top);
      }}
    }});
    ctx.restore();
  }}
}};

// VRAM
const vramDs = [];
Object.entries(GPU).forEach(([id, g], i) => {{
  vramDs.push({{ label: g.name + ' VRAM',
    data: g.ts.map((t, j) => ({{ x: t, y: g.vram[j] }})),
    borderColor: COLORS[i], backgroundColor: COLORS[i] + '20',
    fill: true, tension: 0.2, pointRadius: 0 }});
}});
new Chart('vramChart', {{ type: 'line', data: {{ datasets: vramDs }},
  options: {{ scales: {{ x: timeAxis(), y: {{ beginAtZero: true }} }},
    plugins: {{ legend: {{ position: 'bottom' }} }} }},
  plugins: [stallPlugin] }});

// Util %
const utilDs = [];
Object.entries(GPU).forEach(([id, g], i) => {{
  utilDs.push({{ label: g.name,
    data: g.ts.map((t, j) => ({{ x: t, y: g.util[j] }})),
    borderColor: COLORS[i], tension: 0.2, pointRadius: 0 }});
}});
new Chart('utilChart', {{ type: 'line', data: {{ datasets: utilDs }},
  options: {{ scales: {{ x: timeAxis(), y: {{ beginAtZero: true, max: 100 }} }},
    plugins: {{ legend: {{ position: 'bottom' }} }} }},
  plugins: [stallPlugin] }});

// Temp/Power
const tempDs = [];
Object.entries(GPU).forEach(([id, g], i) => {{
  tempDs.push({{ label: g.name + ' Temp',
    data: g.ts.map((t, j) => ({{ x: t, y: g.temp[j] }})),
    borderColor: COLORS[i], tension: 0.2, pointRadius: 0, yAxisID: 'y' }});
  tempDs.push({{ label: g.name + ' Power',
    data: g.ts.map((t, j) => ({{ x: t, y: g.power[j] }})),
    borderColor: COLORS[i + 2], borderDash: [3, 3], tension: 0.2,
    pointRadius: 0, yAxisID: 'y1' }});
}});
new Chart('tempChart', {{ type: 'line', data: {{ datasets: tempDs }},
  options: {{ scales: {{ x: timeAxis(),
    y: {{ beginAtZero: true, position: 'left', title: {{ display: true, text: 'C' }} }},
    y1: {{ beginAtZero: true, position: 'right', title: {{ display: true, text: 'W' }},
           grid: {{ drawOnChartArea: false }} }} }},
    plugins: {{ legend: {{ position: 'bottom' }} }} }},
  plugins: [stallPlugin] }});

// System metrics
const sysDs = [];
Object.keys(SYS).filter(r => r !== 'host').forEach((role, i) => {{
  const s = SYS[role];
  if (!s || !s.ts.length) return;
  sysDs.push({{ label: role + ' CPU%',
    data: s.ts.map((t, j) => ({{ x: t, y: s.cpu[j] }})),
    borderColor: COLORS[i], tension: 0.2, pointRadius: 0, yAxisID: 'y' }});
  sysDs.push({{ label: role + ' RSS MiB',
    data: s.ts.map((t, j) => ({{ x: t, y: s.rss[j] }})),
    borderColor: COLORS[i + 2], borderDash: [3, 3], tension: 0.2,
    pointRadius: 0, yAxisID: 'y1' }});
}});
new Chart('sysChart', {{ type: 'line', data: {{ datasets: sysDs }},
  options: {{ scales: {{ x: timeAxis(),
    y: {{ beginAtZero: true, position: 'left', title: {{ display: true, text: 'CPU %' }} }},
    y1: {{ beginAtZero: true, position: 'right', title: {{ display: true, text: 'MiB' }},
           grid: {{ drawOnChartArea: false }} }} }},
    plugins: {{ legend: {{ position: 'bottom' }} }} }},
  plugins: [stallPlugin] }});

// Requests bar
if (REQ.hours.length > 0) {{
  new Chart('reqChart', {{ type: 'bar', data: {{
    labels: REQ.hours.map(h => h.replace('T', ' ')),
    datasets: [{{ label: 'Requests', data: REQ.counts, backgroundColor: '#60a5fa80' }}]
  }}, options: {{ scales: {{ x: {{ ticks: {{ maxTicksLimit: 12 }} }} }},
    plugins: {{ legend: {{ display: false }} }} }} }});
}}

// Model pie — uses MODEL_COLOR mapping
if (MODEL.labels.length > 0) {{
  new Chart('modelChart', {{ type: 'doughnut', data: {{
    labels: MODEL.labels,
    datasets: [{{ data: MODEL.counts,
                 backgroundColor: MODEL.labels.map(m => colorForModel(m)) }}]
  }}, options: {{ plugins: {{ legend: {{ position: 'right' }} }} }} }});
}}

// Prompt scatter — one dataset per model so legend names them and colors match
if (PROMPT.ts.length > 0) {{
  const byModel = {{}};
  for (let i = 0; i < PROMPT.ts.length; i++) {{
    const m = PROMPT.model[i] || 'unbekannt';
    (byModel[m] = byModel[m] || []).push({{
      x: PROMPT.tokens[i],
      y: PROMPT.duration[i] / 1000,
      ts: PROMPT.ts[i]
    }});
  }}
  const promptDatasets = Object.entries(byModel).map(([m, pts]) => ({{
    label: m,
    data: pts,
    backgroundColor: colorForModel(m) + 'b3',
    pointRadius: 4
  }}));
  new Chart('promptChart', {{ type: 'scatter', data: {{ datasets: promptDatasets }},
    options: {{
      scales: {{
        x: {{ title: {{ display: true, text: 'Prompt Tokens' }}, beginAtZero: true }},
        y: {{ title: {{ display: true, text: 'Dauer (s)' }}, beginAtZero: true }}
      }},
      plugins: {{
        legend: {{ position: 'bottom' }},
        tooltip: {{
          callbacks: {{
            label(ctx) {{
              const p = ctx.raw;
              return `${{ctx.dataset.label}}: ${{p.x}} tok / ${{p.y.toFixed(2)}}s @ ${{p.ts}}`;
            }}
          }}
        }}
      }}
    }} }});
}}
</script>
</body>
</html>"""
    OUT_PATH.write_text(html)
    return OUT_PATH


def main():
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    p = generate_html(hours)
    print(f"wrote {p}", file=sys.stderr)


if __name__ == "__main__":
    main()
