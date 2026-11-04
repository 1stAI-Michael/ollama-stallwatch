#!/usr/bin/env python3
"""Gestufte Aufbewahrung der Monitoring-Daten (siehe STANDARD.md).

Ersetzt die frühere Cron-Retention, die auf ti-30 **nie gelaufen ist**: der
Eintrag in /etc/cron.d enthielt einen mehrzeiligen Befehl, den das crontab-Format
nicht kennt. Bewiesen wurde das an der Umleitungsdatei — eine Ausgabeumleitung
wird von der Shell *vor* dem Befehl angelegt, also hätte die Logdatei existieren
müssen, selbst wenn der Befehl gescheitert wäre. Sie existierte nicht.

Zwei Lehren stecken deshalb im Aufbau:

1. Der Job läuft aus einer **Skriptdatei** über einen systemd-Timer, nicht als
   Einzeiler in der Crontab.
2. Jeder Lauf schreibt eine Zeile nach `maintenance_log`. Damit ist „hat es
   funktioniert" eine Abfrage und keine Vermutung — `tests/stack_check.py`
   prüft genau das.

Messreihen werden **verdichtet**, nicht ausgedünnt: pro Zeitfenster entsteht eine
Zeile mit Mittelwert UND Maximum. Ereignistabellen (`ollama_requests`,
`stall_events`) werden **nie** verdichtet — sie sind Forensik, keine Statistik.

    python3 retention.py            # regulärer Lauf
    python3 retention.py --trocken  # zeigt nur, was passieren würde
    python3 retention.py --zeigen   # letzte Läufe aus maintenance_log
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import CONFIG, connect  # noqa: E402

TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# Vorgaben des Standards. Fehlt der [retention]-Block in einer config.toml
# (etwa auf einem Host, der vor diesem Standard eingerichtet wurde), greifen
# genau diese Werte — die Datei muss dafür nicht angefasst werden.
STANDARD = {
    "raw_hours": 24,
    "tiers": [(5, 7), (60, 360)],   # (Fensterbreite in Sekunden, Aufbewahrung in Tagen)
    "events_days": 360,
    "stalls_days": 360,
    "vacuum_min_deleted": 100_000,
}


def policy():
    r = dict(CONFIG.get("retention") or {})
    p = dict(STANDARD)
    if "raw_hours" in r:
        p["raw_hours"] = int(r["raw_hours"])
    if "tiers" in r:
        p["tiers"] = [(int(t[0]), int(t[1])) for t in r["tiers"]]
    for k in ("events_days", "stalls_days", "vacuum_min_deleted"):
        if k in r:
            p[k] = int(r[k])
    # Das alte `days = 30` wird BEWUSST ignoriert. Es gehoerte zur Cron-Retention,
    # die nachweislich nie gelaufen ist — der Wert hat nie etwas bewirkt und darf
    # jetzt nicht rueckwirkend 330 Tage Ereignisse loeschen. Wer 30 Tage will,
    # setzt `events_days = 30` ausdruecklich.
    return p


def poll_interval(section: str, default: int) -> int:
    try:
        return int(CONFIG[section]["poll_interval_sec"])
    except (KeyError, TypeError, ValueError):
        return default


def schranke(conn, ausdruck: str) -> str:
    """Zeitschranke im GESPEICHERTEN Textformat liefern.

    NICHT `datetime('now', …)` benutzen: das liefert `2026-08-20 10:45:41`
    (Leerzeichen), gespeichert wird aber `2026-08-20T10:45:41Z`. Verglichen
    wird als TEXT, und 'T' (0x54) ist groesser als ' ' (0x20) — dadurch gilt
    jede Zeile desselben Tages als groesser als jede Schranke desselben Tages,
    unabhaengig von der Uhrzeit. Gemessen am 20.08.2026: dieselbe Abfrage lieferte
    fuer 30 s, 60 s und 300 s identisch 5.889 Zeilen statt 38 / 77 / 113.
    Fuer eine Loeschroutine waere das die gefaehrlichste Sorte Fehler.
    """
    return conn.execute(f"SELECT strftime('{TS_FMT}','now',?)", (ausdruck,)).fetchone()[0]


def bucket_expr(col: str, width: int) -> str:
    """Fensteranfang im selben Textformat wie die Rohzeitstempel."""
    return (f"strftime('{TS_FMT}', (strftime('%s', {col}) / {width}) * {width}, 'unixepoch')")


SERIES = {
    "gpu_metrics": {
        "rollup": "gpu_metrics_rollup",
        "key": ["gpu_id"],
        "quelle_intervall": lambda: poll_interval("gpu", 10),
        # (Spalte im Rollup, Ausdruck auf den Rohdaten, Ausdruck auf einem feineren Rollup)
        "aggregat": [
            ("gpu_name", "gpu_name", "gpu_name"),
            ("samples", "COUNT(*)", "SUM(samples)"),
            ("vram_used_mib_avg", "AVG(vram_used_mib)",
             "SUM(vram_used_mib_avg * samples) / SUM(samples)"),
            ("vram_used_mib_max", "MAX(vram_used_mib)", "MAX(vram_used_mib_max)"),
            ("vram_total_mib", "MAX(vram_total_mib)", "MAX(vram_total_mib)"),
            ("utilization_gpu_avg", "AVG(utilization_gpu)",
             "SUM(utilization_gpu_avg * samples) / SUM(samples)"),
            ("utilization_gpu_max", "MAX(utilization_gpu)", "MAX(utilization_gpu_max)"),
            ("temperature_max", "MAX(temperature)", "MAX(temperature_max)"),
            ("power_draw_w_avg", "AVG(power_draw_w)",
             "SUM(power_draw_w_avg * samples) / SUM(samples)"),
            ("power_draw_w_max", "MAX(power_draw_w)", "MAX(power_draw_w_max)"),
        ],
    },
    "system_metrics": {
        "rollup": "system_metrics_rollup",
        "key": ["proc_role"],
        "quelle_intervall": lambda: poll_interval("process", 10),
        "aggregat": [
            ("samples", "COUNT(*)", "SUM(samples)"),
            ("cpu_percent_avg", "AVG(cpu_percent)",
             "SUM(cpu_percent_avg * samples) / SUM(samples)"),
            ("cpu_percent_max", "MAX(cpu_percent)", "MAX(cpu_percent_max)"),
            ("rss_mib_avg", "AVG(rss_mib)", "SUM(rss_mib_avg * samples) / SUM(samples)"),
            ("rss_mib_max", "MAX(rss_mib)", "MAX(rss_mib_max)"),
            ("num_threads_max", "MAX(num_threads)", "MAX(num_threads_max)"),
            ("host_load1_avg", "AVG(host_load1)",
             "SUM(COALESCE(host_load1_avg,0) * samples) / SUM(samples)"),
            ("host_load1_max", "MAX(host_load1)", "MAX(host_load1_max)"),
            ("host_mem_used_mib_max", "MAX(host_mem_used_mib)", "MAX(host_mem_used_mib_max)"),
        ],
    },
}


# Der Trockenlauf muss zeigen, WAS PASSIEREN WÜRDE — sonst ist er keine
# Vorschau, sondern nur ein teurer Zeilenzähler. Die erste Fassung gab im
# Trockenfall stur `geloescht: 0` zurück; ti11-Ops meldete das an fA-439 mit
# `gelesen: 455430, geloescht: 0` und hielt sich zu Recht nicht daran fest,
# sondern fuhr erst scharf gegen die Sicherungskopie. Nach unserem eigenen
# Maßstab war das eine Prüfung, die im Erfolgsfall aussieht wie im Fehlerfall.
#
# Jetzt gilt: gelöscht wird genau, was gelesen wurde (alles vor dem Schnitt
# wandert ins Fenster und verschwindet danach), und zusätzlich steht da, zu wie
# vielen Fenstern es wird. Die Fenster-Zählung kostet einen zweiten Scan und
# läuft deshalb NUR im Trockenlauf — der scharfe Lauf bleibt so schnell wie
# vorher (gemessen 7,1 s für 3,19 Mio Zeilen).
def verdichte_roh(conn, tabelle, spec, width, cutoff, trocken):
    """Rohzeilen älter als `cutoff` zu Fenstern der Breite `width` zusammenfassen."""
    roll, keys = spec["rollup"], spec["key"]
    ziel = ["bucket_ts", "bucket_sec"] + keys + [a[0] for a in spec["aggregat"]]
    quelle = ([bucket_expr("timestamp", width), str(width)] + keys
              + [a[1] for a in spec["aggregat"]])
    n = conn.execute(
        f"SELECT COUNT(*) FROM {tabelle} WHERE timestamp < ?", (cutoff,)).fetchone()[0]
    if n == 0:
        return 0, 0, 0
    if trocken:
        fenster = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM {tabelle} WHERE timestamp < ? "
            f"GROUP BY {bucket_expr('timestamp', width)}, {', '.join(keys)})",
            (cutoff,)).fetchone()[0]
        return n, n, fenster
    conn.execute(
        f"INSERT OR REPLACE INTO {roll} ({', '.join(ziel)}) "
        f"SELECT {', '.join(quelle)} FROM {tabelle} WHERE timestamp < ? "
        f"GROUP BY 1, {', '.join(keys)}", (cutoff,))
    geloescht = conn.execute(
        f"DELETE FROM {tabelle} WHERE timestamp < ?", (cutoff,)).rowcount
    return n, geloescht, None


def verdichte_stufe(conn, spec, von, nach, cutoff, trocken):
    """Feine Fenster älter als `cutoff` zu gröberen zusammenfassen."""
    roll, keys = spec["rollup"], spec["key"]
    ziel = ["bucket_ts", "bucket_sec"] + keys + [a[0] for a in spec["aggregat"]]
    quelle = ([bucket_expr("bucket_ts", nach), str(nach)] + keys
              + [a[2] for a in spec["aggregat"]])
    n = conn.execute(
        f"SELECT COUNT(*) FROM {roll} WHERE bucket_sec = ? AND bucket_ts < ?",
        (von, cutoff)).fetchone()[0]
    if n == 0:
        return 0, 0, 0
    if trocken:
        fenster = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM {roll} "
            f"WHERE bucket_sec = ? AND bucket_ts < ? "
            f"GROUP BY {bucket_expr('bucket_ts', nach)}, {', '.join(keys)})",
            (von, cutoff)).fetchone()[0]
        return n, n, fenster
    conn.execute(
        f"INSERT OR REPLACE INTO {roll} ({', '.join(ziel)}) "
        f"SELECT {', '.join(quelle)} FROM {roll} WHERE bucket_sec = ? AND bucket_ts < ? "
        f"GROUP BY 1, {', '.join(keys)}", (von, cutoff))
    geloescht = conn.execute(
        f"DELETE FROM {roll} WHERE bucket_sec = ? AND bucket_ts < ?",
        (von, cutoff)).rowcount
    return n, geloescht, None


def lauf(trocken=False):
    p = policy()
    conn = connect()
    t0 = time.time()
    bericht = {"trocken": trocken, "policy": {k: v for k, v in p.items()}, "tabellen": {}}
    geloescht_gesamt = 0

    for tabelle, spec in SERIES.items():
        quelle_iv = spec["quelle_intervall"]()
        # Ein Fenster kann nie feiner sein als die Quelle, aus der es entsteht.
        # Bei 10s-Abtastung ist eine 5s-Stufe sinnlos und wird uebersprungen.
        stufen = [(w, d) for (w, d) in p["tiers"] if w >= quelle_iv]
        uebersprungen = [w for (w, d) in p["tiers"] if w < quelle_iv]
        eintrag = {"quelle_intervall_sec": quelle_iv,
                   "stufen_uebersprungen": uebersprungen, "schritte": []}

        if not stufen:
            eintrag["hinweis"] = "keine Stufe grob genug — Rohdaten bleiben unangetastet"
            bericht["tabellen"][tabelle] = eintrag
            continue

        cutoff = schranke(conn, f"-{p['raw_hours']} hours")
        gelesen, weg, fenster = verdichte_roh(
            conn, tabelle, spec, stufen[0][0], cutoff, trocken)
        geloescht_gesamt += weg
        schritt = {"von": "roh", "nach_sec": stufen[0][0], "aelter_als": cutoff,
                   "gelesen": gelesen, "geloescht": weg}
        if fenster is not None:
            schritt["verdichtet_zu_fenstern"] = fenster
        eintrag["schritte"].append(schritt)

        for i in range(len(stufen) - 1):
            (w_von, d_von), (w_nach, _) = stufen[i], stufen[i + 1]
            c = schranke(conn, f"-{d_von} days")
            gelesen, weg, fenster = verdichte_stufe(conn, spec, w_von, w_nach, c, trocken)
            geloescht_gesamt += weg
            schritt = {"von_sec": w_von, "nach_sec": w_nach, "aelter_als": c,
                       "gelesen": gelesen, "geloescht": weg}
            if fenster is not None:
                schritt["verdichtet_zu_fenstern"] = fenster
            eintrag["schritte"].append(schritt)

        w_letzte, d_letzte = stufen[-1]
        c = schranke(conn, f"-{d_letzte} days")
        if trocken:
            weg = conn.execute(
                f"SELECT COUNT(*) FROM {spec['rollup']} WHERE bucket_sec=? AND bucket_ts<?",
                (w_letzte, c)).fetchone()[0]
        else:
            weg = conn.execute(
                f"DELETE FROM {spec['rollup']} WHERE bucket_sec=? AND bucket_ts<?",
                (w_letzte, c)).rowcount
        geloescht_gesamt += weg
        eintrag["schritte"].append(
            {"endgueltig_geloescht_sec": w_letzte, "aelter_als": c, "zeilen": weg})
        bericht["tabellen"][tabelle] = eintrag

    # Ereignisse: nie verdichten, nur sehr spaet loeschen.
    #
    # fA-1833 (gemessen 03.11.2026): `proxy_requests` kam hier null mal vor und ist damit
    # unbegrenzt gewachsen - die Tabelle wurde mit dem Logproxy angebaut (fA-338) und beim
    # Aufraeumen vergessen. Sie traegt Zeitpunkt, Agentenkennung, Client-Adresse, Endpunkt,
    # Modell und Tokenzahlen, KEINE Anfrageinhalte (mit Koedern ueber Erfolgs- und Fehlerpfad
    # gemessen, fA-1485), gehoert also in dieselbe Klasse wie `ollama_requests` und bekommt
    # dieselbe Frist.
    #
    # Nach VORHANDENSEIN gefiltert, nicht nach Namensliste: den Logproxy gibt es nicht auf
    # jedem Host der Flotte, und ein DELETE auf eine fehlende Tabelle wuerde den ganzen
    # Aufraeumlauf reissen - also genau die Wartung, die nie scheitern darf.
    vorhanden = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    ereignis_tabellen = [(t, sp) for t, sp in (("ollama_requests", "timestamp"),
                                               ("stall_events", "start_ts"),
                                               ("proxy_requests", "timestamp"))
                         if t in vorhanden]
    bericht["ereignis_tabellen_gefunden"] = [t for t, _ in ereignis_tabellen]
    for tabelle, spalte in ereignis_tabellen:
        c = schranke(conn, f"-{p['events_days']} days")
        if trocken:
            n = conn.execute(
                f"SELECT COUNT(*) FROM {tabelle} WHERE {spalte} < ?", (c,)).fetchone()[0]
        else:
            n = conn.execute(
                f"DELETE FROM {tabelle} WHERE {spalte} < ?", (c,)).rowcount
        geloescht_gesamt += n
        bericht["tabellen"][tabelle] = {"unveraendert_aufbewahrt_tage": p["events_days"],
                                        "geloescht": n}

    # Stack-Dumps auf der Platte
    stalls = Path(CONFIG["stalls_dir"])
    grenze = time.time() - p["stalls_days"] * 86400
    dateien = [f for f in stalls.glob("*.txt") if f.stat().st_mtime < grenze] if stalls.is_dir() else []
    if not trocken:
        for f in dateien:
            f.unlink()
    bericht["stack_dumps_geloescht"] = len(dateien)

    if not trocken and geloescht_gesamt >= p["vacuum_min_deleted"]:
        # VACUUM sperrt die Datei exklusiv — deshalb nur, wenn wirklich viel
        # frei wurde, sonst blockiert es die Collectoren ohne Gegenwert.
        # Es darf ausserdem NICHT den ganzen Lauf reissen: die Verdichtung ist
        # zu dem Zeitpunkt bereits erledigt und festgeschrieben, und ein
        # nicht verkleinerter Datei-Umfang ist kein Datenverlust.
        t_v = time.time()
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("VACUUM")
            bericht["vacuum_s"] = round(time.time() - t_v, 1)
        except Exception as e:
            bericht["vacuum_fehler"] = repr(e)

    dauer = round(time.time() - t0, 2)
    bericht["geloescht_gesamt"] = geloescht_gesamt
    if not trocken:
        conn.execute(
            "INSERT INTO maintenance_log (ts, job, ok, duration_s, details) "
            "VALUES (strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'retention', 1, ?, ?)",
            (dauer, json.dumps(bericht, ensure_ascii=False)))
    conn.close()
    bericht["dauer_s"] = dauer
    return bericht


def zeigen():
    conn = connect()
    rows = conn.execute(
        "SELECT ts, ok, duration_s, details FROM maintenance_log "
        "WHERE job='retention' ORDER BY ts DESC LIMIT 10").fetchall()
    if not rows:
        print("Noch kein Retention-Lauf verzeichnet.")
        return 1
    for ts, ok, dur, det in rows:
        try:
            n = json.loads(det).get("geloescht_gesamt", "?")
        except Exception:
            n = "?"
        print(f"{ts}  ok={ok}  {dur:>6.2f}s  geloescht={n}")
    conn.close()
    return 0


def lauf_protokolliert(trocken=False):
    """Wie lauf(), schreibt aber AUCH bei einem Fehlschlag eine Protokollzeile.

    Ohne das waere ein gescheiterter Lauf von einem nie gestarteten nicht zu
    unterscheiden — und genau diese Verwechslung ist der Grund, warum die alte
    Retention 146 Tage lang unbemerkt nicht lief.
    """
    t0 = time.time()
    try:
        return lauf(trocken=trocken), 0
    except Exception as e:
        if not trocken:
            try:
                conn = connect()
                conn.execute(
                    "INSERT INTO maintenance_log (ts, job, ok, duration_s, details) "
                    "VALUES (strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'retention', 0, ?, ?)",
                    (round(time.time() - t0, 2),
                     json.dumps({"fehler": repr(e)}, ensure_ascii=False)))
                conn.close()
            except Exception:
                pass
        return {"ok": False, "fehler": repr(e)}, 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trocken", action="store_true",
                    help="nur zeigen, was passieren wuerde — schreibt nichts")
    ap.add_argument("--zeigen", action="store_true",
                    help="letzte Laeufe aus maintenance_log anzeigen")
    a = ap.parse_args()
    if a.zeigen:
        raise SystemExit(zeigen())
    b, rc = lauf_protokolliert(trocken=a.trocken)
    print(json.dumps(b, indent=2, ensure_ascii=False))
    raise SystemExit(rc)
