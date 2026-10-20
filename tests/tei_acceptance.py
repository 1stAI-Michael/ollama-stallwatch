#!/usr/bin/env python3
"""
TEI CPU-Backup Acceptance-Test — vergleicht TI-30 gegen GX-10-Referenz.

Kriterien (aus 2026-07-07_SPEC_embedding-reranker_fuer-TI-30-CPU-backup_from_gx10-01.md):
  Embedding:  Dimension == 1024, ||v|| ≈ 1.0, Cosine-Sim(TI-30, GX-10) ≥ 0.98
  Reranker:   gleiche Rangfolge, Score-Größenordnung im selben Bereich (roh, negativ möglich)

Exit-Code 0 = alle Assertions bestanden, 1 = mindestens eine gescheitert,
2 = nicht anwendbar, weil tei-embed auf ti-30 abgeschaltet ist (seit 20.10.2026,
fA-1311/fA-1575). 2 ist ausdruecklich NICHT 0: der Embedding-Teil wurde dann nicht
geprueft, und ein nicht gelaufener Test darf nicht wie ein bestandener aussehen.
"""

import json
import math
import sys
import time
import urllib.error
import urllib.request

# gx10-01 ueber Tailscale, nicht ueber die LAN-Adresse: ueberlebt den Umzug nach
# 192.168.7.0/24 (fA-508). Bitte NICHT auf eine 192.168.x-Adresse zurueckstellen.
# gx10-01 via Tailscale, not its LAN address — survives the move (fA-508).
GX10_EMBED = "http://100.123.138.47:8001/v1/embeddings"
GX10_RERANK = "http://100.123.138.47:8082/v1/rerank"
TI30_EMBED = "http://localhost:8001/v1/embeddings"
# rerank-adapter davor spricht dasselbe /v1/rerank-Format wie GX-10.
TI30_RERANK = "http://localhost:8082/v1/rerank"

SAMPLES = [
    "Patient came in with chest pain and shortness of breath.",
    "Diagnose: akute Bronchitis, Antibiotikatherapie eingeleitet.",
    "COBOL Copybook PIC 9(5)V99 COMP-3 verwendet packed decimal.",
    "Der Junior-Chatbot benoetigt Kontext-Behaltung fuer Programm-Analyse.",
    "Recipe: sift flour, add butter, bake at 180 degrees for 40 minutes.",
]

RERANK_QUERY = "medical transcription of a patient visit"
RERANK_DOCS = [
    "Patient came in with chest pain and shortness of breath.",
    "Recipe: sift flour, add butter, bake at 180 degrees.",
    "Doctor recorded vital signs and prescribed medication for the patient.",
    "COBOL Copybook uses packed decimal encoding.",
]


def post_json(url: str, payload: dict, timeout: float = 60.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get(url: str, timeout: float = 5.0) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except urllib.error.URLError:
        return 0


def l2_norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) / (l2_norm(a) * l2_norm(b))


def embed_one(url: str, text: str) -> tuple[list[float], float]:
    t0 = time.perf_counter()
    r = post_json(url, {"model": "bge-m3", "input": text})
    dt = time.perf_counter() - t0
    return r["data"][0]["embedding"], dt


def rerank(url: str, query: str, docs: list[str]) -> tuple[list[dict], float]:
    """GX-10 und TI-30 (via rerank-adapter) sprechen dasselbe /v1/rerank-Format."""
    t0 = time.perf_counter()
    r = post_json(url, {"model": "bge-reranker-v2-m3", "query": query, "documents": docs})
    dt = time.perf_counter() - t0
    return r["results"], dt


def main() -> int:
    fails: list[str] = []
    results: dict = {"embedding": [], "reranker": {}}

    print("=" * 78)
    print("TEI CPU-Backup Acceptance-Test — TI-30 vs. GX-10")
    print("=" * 78)

    # Der Embedding-Teil dieses Tests braucht tei-embed auf :8001. Dieser Dienst ist auf
    # ti-30 seit dem 20.10.2026 abgeschaltet (fA-1311/fA-1575): das Flows-Lastmanagement
    # fuehrt den Endpunkt seit 13.10. als enabled=FALSE und zaehlte 0 Anfragen in 30 Tagen.
    # Darum hier ein eigener Ausgang 2 statt eines Fehlschlags (1) ODER eines stillen
    # Ueberspringens: ein uebersprungener Teil, der wie ein bestandener aussieht, ist genau
    # die Falle aus fA-648. Exit 2 heisst "nicht anwendbar" und ist weder gruen noch rot.
    if get("http://localhost:8001/health") != 200:
        print("\n  TI-30 embed (:8001) antwortet nicht.")
        print("  Das ist der ERWARTETE Zustand seit 20.10.2026 — tei-embed ist abgeschaltet")
        print("  (fA-1311/fA-1575, Compose-Profil \"aus\"). Der Embedding-Teil wird damit")
        print("  NICHT geprueft; der Reranker-Teil braucht diesen Dienst nicht.")
        print("  Zum Pruefen vorher einschalten:")
        print("    docker compose --profile aus up -d tei-embed")
        print("\n  Ergebnis: NICHT ANWENDBAR (Exit 2) — nicht als bestanden lesen.")
        return 2

    print("\n[1/4] Health-Probe beide Systeme")
    for label, url in [("GX-10 embed", "http://100.123.138.47:8001/health"),
                       ("GX-10 rerank", "http://100.123.138.47:8082/health"),
                       ("TI-30 embed", "http://localhost:8001/health"),
                       ("TI-30 rerank", "http://localhost:8082/health")]:
        status = get(url)
        ok = status == 200
        print(f"  {label:20} {url:45} {'OK' if ok else f'FAIL ({status})'}")
        if not ok:
            fails.append(f"health {label} = {status}")

    if fails:
        print("\nHealth-Probe fehlgeschlagen — Abbruch.")
        return 1

    print("\n[2/4] Embedding — Dimension, L2-Norm, Cosine-Sim")
    print(f"  {'Text':60} dim   ‖v‖GX10  ‖v‖TI30  cosine  Δms")
    for text in SAMPLES:
        v_gx, dt_gx = embed_one(GX10_EMBED, text)
        v_ti, dt_ti = embed_one(TI30_EMBED, text)
        dim = len(v_gx)
        norm_gx = l2_norm(v_gx)
        norm_ti = l2_norm(v_ti)
        cos = cosine(v_gx, v_ti)
        results["embedding"].append({
            "text": text, "dim": dim, "norm_gx": norm_gx, "norm_ti": norm_ti,
            "cosine": cos, "dt_gx_ms": dt_gx * 1000, "dt_ti_ms": dt_ti * 1000,
        })
        marker = "✓" if (dim == 1024 and abs(norm_ti - 1.0) < 0.01 and cos >= 0.98) else "✗"
        print(f"  {marker} {text[:58]:60} {dim:4} {norm_gx:7.4f} {norm_ti:7.4f} {cos:6.4f} {int(dt_ti * 1000)}")
        if dim != 1024:
            fails.append(f"embed dim {dim} != 1024 for: {text[:40]}")
        if abs(norm_ti - 1.0) > 0.01:
            fails.append(f"embed TI30 norm {norm_ti:.4f} not L2-normalized: {text[:40]}")
        if cos < 0.98:
            fails.append(f"embed cosine {cos:.4f} < 0.98 for: {text[:40]}")

    print("\n[3/4] Reranker — Rangfolge + Score-Größenordnung")
    r_gx, dt_gx = rerank(GX10_RERANK, RERANK_QUERY, RERANK_DOCS)
    r_ti, dt_ti = rerank(TI30_RERANK, RERANK_QUERY, RERANK_DOCS)
    order_gx = [x["index"] for x in r_gx]
    order_ti = [x["index"] for x in r_ti]
    scores_gx = {x["index"]: x["relevance_score"] for x in r_gx}
    scores_ti = {x["index"]: x["relevance_score"] for x in r_ti}
    results["reranker"] = {
        "query": RERANK_QUERY, "docs": RERANK_DOCS,
        "order_gx": order_gx, "order_ti": order_ti,
        "scores_gx": scores_gx, "scores_ti": scores_ti,
        "dt_gx_ms": dt_gx * 1000, "dt_ti_ms": dt_ti * 1000,
    }
    print(f"  Query: {RERANK_QUERY}")
    print(f"  Rangfolge GX-10: {order_gx}")
    print(f"  Rangfolge TI-30: {order_ti}")
    print(f"  {'Doc':60} score-GX  score-TI  Δscore")
    for idx in range(len(RERANK_DOCS)):
        sg, st = scores_gx[idx], scores_ti[idx]
        print(f"  [{idx}] {RERANK_DOCS[idx][:56]:56} {sg:+8.3f}  {st:+8.3f}  {abs(sg - st):5.2f}")
    print(f"  Latenz GX-10: {int(dt_gx * 1000)} ms · TI-30: {int(dt_ti * 1000)} ms")
    if order_gx != order_ti:
        fails.append(f"rerank Rangfolge weicht ab: GX10={order_gx} TI30={order_ti}")
    # Score-Skala: TI-30 muss auch negative Werte im selben Bereich haben (nicht [0,1]).
    top_gx = scores_gx[order_gx[0]]
    top_ti = scores_ti[order_ti[0]]
    if not (top_gx > 0 or top_gx < 0):
        fails.append("rerank GX-10 score not a number")
    if abs(top_ti) < 0.01 and abs(top_gx) > 0.5:
        fails.append(f"rerank TI-30 score ~0 while GX-10 is {top_gx:.3f} — Skala verdächtig")
    if 0 <= top_ti <= 1 and top_gx < 0:
        fails.append(f"rerank TI-30 top={top_ti:.3f} in [0,1], GX-10 top={top_gx:.3f} negativ — Sigmoid vs. roh?")

    print("\n[4/4] Ergebnis")
    if fails:
        print("FAIL — folgende Assertions gescheitert:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS — alle Kriterien erfüllt.")
    with open("tests/tei_acceptance_results.json", "w") as fh:
        json.dump(results, fh, indent=2)
    print("Ergebnisse: tests/tei_acceptance_results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
