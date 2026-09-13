#!/usr/bin/env python3
"""Stack-Check TI-30 — ist die Maschine im Soll-Zustand?

Gedacht als Zwei-Minuten-Prüfung vor dem Betriebstag und nach jedem Eingriff.
Prüft nicht nur, ob Dienste laufen, sondern ob die Maschine tatsächlich
antwortet — ein Container "healthy" und ein `/api/tags` 200 sagen nichts
darüber, ob ein Modell noch lädt und Text erzeugt.

Bewusst enthalten, weil auf dieser Maschine dreimal ein konfigurierter Wert
still nicht eingehalten wurde (Kontext-Deckelung, NUM_PARALLEL, TEI
--auto-truncate): der Check liest die Soll-Werte NICHT nur aus der
Konfiguration, sondern misst nach, wo das billig geht.

    python3 tests/stack_check.py            # voller Lauf
    python3 tests/stack_check.py --schnell  # ohne Inferenz (kein Modell-Load)

Exit 0 = alles im Soll · 1 = mindestens eine harte Prüfung gerissen
       · 2 = nur weiche Hinweise.

Auf mehreren Hosts (fA-439)
---------------------------
Nichts hier ist mehr auf ti-30 festgenagelt. Pfade kommen aus
`monitoring/config.toml`, Container- und Modellnamen aus dessen optionalem
`[stack_check]`-Block; fehlt er, gelten die ti-30-Werte als Vorgabe.

Der Grund ist ein Befund von ti11-Ops: dort rissen 8 von 25 Prüfungen, **alle
acht waren ti-30-Annahmen** — andere Containernamen, das Repo eine Ebene tiefer
als das hartkodierte `/srv/Container`, Listen- statt Abbildungs-Syntax in der
Compose-Datei. Der laufende Container hatte dabei jeden einzelnen Sollwert
exakt. Das Skript meldete trotzdem „NICHT BETRIEBSBEREIT:
OLLAMA_CONTEXT_LENGTH = 131072" — und las sich damit wie die Aufforderung, an
einer korrekten Produktivkonfiguration zu drehen. Ein Check, der zum Eingriff
in einen gesunden Zustand auffordert, ist schädlicher als gar kein Check.

Deshalb wird der Ist-Zustand jetzt am **laufenden Container** gemessen
(`docker exec … env`) statt aus der Compose-Datei gelesen. Die Compose-Datei ist
die Absicht, der Prozess ist die Wirklichkeit — und nur die zweite trägt den
Betrieb. Die Datei bleibt als weicher Abgleich daneben stehen.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MONITORING = REPO / "monitoring"

# ti-30-Vorgaben. Sie gelten nur, solange config.toml nichts anderes sagt.
STANDARD = {
    "container": ["ollama", "openwebui", "tei-embed", "tei-rerank", "rerank-adapter",
                  "ollama-logproxy"],
    "dienste": ["ollama-gpu-logger", "ollama-log-parser", "ollama-process-logger",
                "ollama-stall-detector", "ollama-dashboard"],
    # Modelle, die den Betrieb tragen. Klinisch = das Modell, dessen Antworten
    # sich nicht ändern dürfen; agentisch = das Modell für Studio-/Agenten-Last.
    "modell_klinisch": "alibayram/medgemma:27b",
    "modell_agentisch": "qwen3.6:35b-a3b-q4_K_M",
    # Weichen diese Werte ab, hat jemand die Maschine umkonfiguriert —
    # unabhängig davon, ob das absichtlich war.
    "soll_env": {
        "OLLAMA_CONTEXT_LENGTH": "131072",
        "OLLAMA_NUM_PARALLEL": "2",
        "OLLAMA_MAX_LOADED_MODELS": "2",
        "OLLAMA_FLASH_ATTENTION": "1",
    },
    # Flotten-Standard, siehe monitoring/STANDARD.md §1.
    "log_max_size": "25m",
    "log_max_file": "4",
}


def lade_config() -> dict:
    """config.toml nur lesen — ohne Seiteneffekte.

    Bewusst NICHT über `monitoring/db.py`: dessen `connect()` legt beim Import
    Tabellen an und braucht Schreibrecht. Ein Zustands-Check darf den Zustand
    nicht verändern, den er prüft.
    """
    pfad = MONITORING / "config.toml"
    if not pfad.exists():
        return {}
    try:
        with open(pfad, "rb") as f:
            return tomllib.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] {pfad} nicht lesbar ({type(e).__name__}: {e}) — "
              f"es gelten die eingebauten ti-30-Vorgaben")
        return {}


CFG = lade_config()
SC = CFG.get("stack_check", {})


def _pfad(p: str) -> Path:
    """Relative Angaben lösen gegen monitoring/ auf — wie in db.py."""
    q = Path(p)
    return q if q.is_absolute() else (MONITORING / q).resolve()


DB = _pfad(CFG.get("db_path", "monitor.db"))
COMPOSE = _pfad(CFG.get("ollama", {}).get("compose_file", "../docker-compose.yml"))
OLLAMA_CONTAINER = CFG.get("ollama", {}).get("container_name", "ollama")
OLLAMA = SC.get("ollama_url", "http://127.0.0.1:11434")
LOGPROXY = SC.get("logproxy_url", "http://127.0.0.1:11435")
# Dem konfigurierten Bind folgen statt 127.0.0.1 anzunehmen (fA-850). Seit das
# Dashboard ans Tailnet gebunden ist, antwortet localhost nicht mehr — eine
# Pruefung, die weiter dorthin fragt, meldet einen gesunden Dienst als tot und
# schickt jemanden auf die falsche Suche. `0.0.0.0` ist keine Zieladresse: dort
# lauscht es ueberall, und localhost ist dann der kuerzeste Weg hin.
_API_HOST = CFG.get("api", {}).get("host", "127.0.0.1")
if _API_HOST in ("0.0.0.0", "::", ""):
    _API_HOST = "127.0.0.1"
DASHBOARD = f"http://{_API_HOST}:{CFG.get('api', {}).get('port', 3002)}"

CONTAINER = SC.get("container", STANDARD["container"])
# Optionale Wahl-Sets je Betriebsmodus; ohne den Block ändert sich nichts.
MODI = SC.get("modi", {})
DIENSTE = SC.get("dienste", STANDARD["dienste"])
MODELL_KLINISCH = SC.get("modell_klinisch", STANDARD["modell_klinisch"])
MODELL_AGENTISCH = SC.get("modell_agentisch", STANDARD["modell_agentisch"])
SOLL_ENV = SC.get("soll_env", STANDARD["soll_env"])
LOG_MAX_SIZE = SC.get("log_max_size", STANDARD["log_max_size"])
LOG_MAX_FILE = str(SC.get("log_max_file", STANDARD["log_max_file"]))

ERG: list[dict] = []


def pruefe(name: str, hart: bool, ok: bool, detail: str = "") -> bool:
    ERG.append({"name": name, "hart": hart, "ok": ok, "detail": detail})
    marke = "OK  " if ok else ("FAIL" if hart else "WARN")
    print(f"  [{marke}] {name}" + (f"  —  {detail}" if detail else ""))
    return ok


def sh(*args: str, timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:  # noqa: BLE001 — jede Störung ist hier ein Fail
        return 1, f"{type(e).__name__}: {e}"


def hole(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def poste(url: str, body: dict, timeout: float = 900.0):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode()), time.time() - t0


# ------------------------------------------------------------------ Container
def check_container() -> None:
    """`container` ist Pflicht, `[stack_check.modi]` sind Wahl-Sets.

    Hintergrund (ti11-Ops, fA-439): auf ti11 teilen sich zwei Container-Sets
    dieselbe Karte, `stack-mode.sh` tauscht sie gegeneinander. Eine feste Liste
    meldet nach jedem Moduswechsel Fehler — zu Recht, aber aus dem falschen
    Grund. Ein abgeschaltetes Set ist kein Defekt.

    Bewusst NICHT über einen Schlüssel `modus = "diwa"` gelöst. Das wäre ein
    Wert, den jemand beim Umschalten mitpflegen muss und der still veraltet —
    genau die Fehlerklasse, die dieses Skript loswerden sollte. Stattdessen
    wird der laufende Zustand befragt, und die drei Fälle werden unterschieden:

      alle da   -> in Ordnung, dieses Set ist aktiv
      keiner da -> Hinweis: anderes Set aktiv, kein Fehler
      teils da  -> HARTER FEHLER. Ein halb laufendes Set ist kein Moduswechsel,
                   sondern ein Set im aktiven Modus, aus dem etwas weggebrochen
                   ist — der Fall, den zu verschweigen am teuersten wäre.
    """
    print("\nContainer")
    if shutil.which("docker") is None:
        pruefe("docker vorhanden", True, False, "docker nicht im PATH")
        return
    rc, out = sh("docker", "ps", "--format", "{{.Names}}\t{{.Status}}")
    laufend = dict(z.split("\t", 1) for z in out.splitlines() if "\t" in z)

    def laeuft(name: str) -> bool:
        return laufend.get(name, "").startswith("Up")

    for c in CONTAINER:
        st = laufend.get(c, "")
        pruefe(f"Container {c}", True, st.startswith("Up"), st or "läuft nicht")

    for name, satz in MODI.items():
        da = [c for c in satz if laeuft(c)]
        fehlt = [c for c in satz if not laeuft(c)]
        if not fehlt:
            pruefe(f"Set '{name}' vollständig", True, True, ", ".join(satz))
        elif not da:
            pruefe(f"Set '{name}' läuft nicht", False, False,
                   f"anderer Modus aktiv? keiner von: {', '.join(satz)}")
        else:
            pruefe(f"Set '{name}' nur teilweise", True, False,
                   f"läuft: {', '.join(da)} — fehlt: {', '.join(fehlt)}")


def check_dienste() -> None:
    print("\nMonitoring-Dienste")
    for d in DIENSTE:
        rc, out = sh("systemctl", "is-active", d)
        pruefe(f"systemd {d}", True, out == "active", out)


# ------------------------------------------------------- Konfigurations-Drift
# Ollama nimmt für Schalter mehrere Schreibweisen an. `FLASH_ATTENTION=1` und
# `=true` sind derselbe Zustand — ti11 fährt die zweite Fassung. Ein Check, der
# hier auf Textgleichheit besteht, meldet eine Abweichung, die keine ist.
WAHR = {"1", "true", "yes", "on", "t"}
FALSCH = {"0", "false", "no", "off", "f"}


def gleichwertig(ist: str, soll: str) -> bool:
    a, b = ist.strip().strip('"').lower(), soll.strip().strip('"').lower()
    if a == b:
        return True
    if a in WAHR and b in WAHR:
        return True
    if a in FALSCH and b in FALSCH:
        return True
    try:                       # "131072" == "131072.0" == " 131072 "
        return float(a) == float(b)
    except ValueError:
        return False


def check_konfig() -> None:
    """Soll-Werte am LAUFENDEN Container messen, nicht in der Datei lesen.

    Die Compose-Datei ist die Absicht, der Prozess ist die Wirklichkeit. Auf
    dieser Maschine sind sie dreimal auseinandergefallen, ohne dass etwas
    scheiterte. Und die Datei allein zu lesen ist auf einem anderen Host schlicht
    falsch: ti11 schreibt `- SCHLUESSEL=${WERT}` mit den Werten in `.env`, wo
    dieser Check früher `SCHLUESSEL:` suchte und „fehlt" meldete (fA-439).
    """
    print("\nKonfiguration (gemessen am laufenden Container)")
    rc, out = sh("docker", "exec", OLLAMA_CONTAINER, "env", timeout=30)
    if rc != 0:
        pruefe(f"env aus Container {OLLAMA_CONTAINER} lesbar", True, False, out[:120])
        return
    ist_env = dict(z.split("=", 1) for z in out.splitlines() if "=" in z)
    for schluessel, soll in SOLL_ENV.items():
        ist = ist_env.get(schluessel, "")
        pruefe(f"{schluessel} = {soll}", True, gleichwertig(ist, str(soll)),
               f"ist: {ist or 'fehlt'}")

    # Die Datei daneben — WEICH. Eine Abweichung ist ein Hinweis, dass der
    # nächste Neubau des Containers etwas anderes tut als der laufende; sie ist
    # kein Grund, den gesunden Betrieb als „nicht betriebsbereit" zu melden.
    rc, out = sh("git", "-C", str(REPO), "status", "--porcelain", "docker-compose.yml")
    if rc != 0 and "not a git repository" in out.lower():
        pruefe("docker-compose.yml unverändert gegenüber git", False, True,
               "kein git-Repo — Abgleich entfällt")
    else:
        pruefe("docker-compose.yml unverändert gegenüber git", False, out == "",
               "lokal geändert: " + out if out else "sauber")


def check_log_deckel() -> None:
    """Greift der 100-MB-Deckel wirklich? (Flotten-Standard, STANDARD.md §1)

    `json-file` rotiert von Haus aus NICHT, und der Deckel greift erst beim
    NEUERZEUGEN eines Containers — ein Neustart genügt nicht. Wer die Option
    setzt und die Container nur neu startet, glaubt sich abgesichert und ist es
    nicht.

    Gelesen wird als JSON, nicht als Textmuster. Die erste Fassung dieses Checks
    (in `nach-reboot.sh`) prüfte mit dem Glob `*25m*4*` und meldete am 20.08. an
    fünf korrekt konfigurierten Containern einen Fehler: Docker gibt `max-file`
    VOR `max-size` aus, das Muster verlangte die andere Reihenfolge.
    """
    print("\nLog-Deckel")
    for c in CONTAINER:
        rc, out = sh("docker", "inspect", c, "--format", "{{json .HostConfig.LogConfig}}")
        if rc != 0:
            pruefe(f"Log-Deckel {c}", False, False, "Container nicht vorhanden")
            continue
        try:
            conf = (json.loads(out) or {}).get("Config") or {}
        except json.JSONDecodeError:
            pruefe(f"Log-Deckel {c}", False, False, f"unlesbar: {out[:60]}")
            continue
        groesse, dateien = conf.get("max-size", ""), conf.get("max-file", "")
        pruefe(f"Log-Deckel {c}", False,
               groesse == LOG_MAX_SIZE and dateien == LOG_MAX_FILE,
               f"max-size={groesse or 'fehlt'} max-file={dateien or 'fehlt'}"
               + ("" if groesse else " — Container neu erzeugen, nicht neu starten"))


# --------------------------------------------------------------------- Ollama
def check_ollama(schnell: bool) -> None:
    print("\nOllama")
    try:
        tags = hole(OLLAMA + "/api/tags")
    except Exception as e:  # noqa: BLE001
        pruefe("Ollama erreichbar", True, False, f"{type(e).__name__}: {e}")
        return
    namen = {m["name"] for m in tags.get("models", [])}
    pruefe("Ollama erreichbar", True, True, f"{len(namen)} Modelle")
    pruefe(f"Modell vorhanden: {MODELL_KLINISCH}", True, MODELL_KLINISCH in namen)
    pruefe(f"Modell vorhanden: {MODELL_AGENTISCH}", True, MODELL_AGENTISCH in namen)

    if schnell:
        print("       (--schnell: Inferenz übersprungen)")
        return

    # Der eigentliche Punkt: erzeugt die Maschine Text? Ein Modell-Kaltstart
    # kostet hier gemessen ~92 s, deshalb ist das Timeout großzügig.
    body = {"model": MODELL_KLINISCH, "stream": False, "think": False,
            "messages": [{"role": "user", "content": "Antworte mit genau einem Wort: bereit"}],
            "options": {"num_predict": 12, "temperature": 0}}
    try:
        d, w = poste(OLLAMA + "/api/chat", body)
    except Exception as e:  # noqa: BLE001
        pruefe("klinisches Modell antwortet", True, False, f"{type(e).__name__}: {e}")
        return
    inhalt = ((d.get("message") or {}).get("content") or "").strip()
    pruefe("klinisches Modell antwortet", True, bool(inhalt),
           f"{w:.1f}s, {len(inhalt)} Zeichen: {inhalt[:40]!r}")
    # Kaltstart ist kein Fehler, aber der Grund, warum der erste Request des
    # Tages langsam ist — hier sichtbar machen statt später rätseln.
    pruefe("Antwortzeit unter 30 s (sonst Kaltstart)", False, w < 30, f"{w:.1f}s")


# ------------------------------------------------------------- Logging-Proxy
def check_logproxy() -> None:
    """Der Proxy (fA-338 ②) ist additiv — faellt er aus, laeuft die Inferenz
    weiter und nur die Zuordnung hoert auf. Genau deshalb gehoert er geprueft:
    ein Ausfall sieht von aussen aus wie Normalbetrieb, und die Luecke faellt
    erst auf, wenn jemand Monate spaeter fragt, wer die Maschinenzeit verbraucht
    hat (vgl. den Retention-Cron, der 146 Tage lang nicht lief).

    Zwei Stufen, weil „Port antwortet" zu wenig beweist:

      1. antwortet 11435 dasselbe wie 11434? -> es ist wirklich ein Proxy und
         nicht irgendein Dienst, der zufaellig auf dem Port sitzt
      2. entsteht dabei eine Zeile in proxy_requests? -> die Buchhaltung
         schreibt tatsaechlich, nicht nur der Datenpfad laeuft

    Beides ohne Modell-Ladung, also auch unter --schnell fahrbar.
    """
    print("\nLogging-Proxy (Agenten-Zuordnung)")
    try:
        oben = hole(OLLAMA + "/api/version", timeout=5)
    except Exception as e:  # noqa: BLE001
        pruefe("Proxy vergleichbar mit Direktweg", False, False,
               f"Direktweg antwortet nicht: {type(e).__name__}")
        return

    try:
        with sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10) as c:
            vorher = c.execute("SELECT COUNT(*) FROM proxy_requests").fetchone()[0]
    except sqlite3.Error as e:
        vorher = None
        pruefe("Tabelle proxy_requests vorhanden", False, False, str(e))

    try:
        unten = hole(LOGPROXY + "/api/version", timeout=5)
    except Exception as e:  # noqa: BLE001
        pruefe("Proxy erreichbar", True, False, f"{LOGPROXY}: {type(e).__name__}: {e}")
        return
    pruefe("Proxy reicht durch", True, unten == oben,
           f"11435 {unten.get('version')!r} == 11434 {oben.get('version')!r}")

    if vorher is None:
        return
    # Der Proxy schreibt die Zeile ERST, nachdem er die Antwort rausgeschickt
    # hat (Buchhaltung darf den Datenpfad nicht aufhalten). Wer direkt nach dem
    # Request zaehlt, liest deshalb zu frueh und bekommt einen Warnhinweis, der
    # nichts bedeutet. Kurz nachfassen statt einmal raten.
    nachher = vorher
    for _ in range(20):
        try:
            with sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10) as c:
                nachher = c.execute("SELECT COUNT(*) FROM proxy_requests").fetchone()[0]
        except sqlite3.Error as e:
            pruefe("Proxy schreibt mit", False, False, str(e))
            return
        if nachher > vorher:
            break
        time.sleep(0.1)
    # Weich: ein verlorener Zaehler kostet Buchhaltung, nicht Betrieb.
    pruefe("Proxy schreibt mit", False, nachher > vorher,
           f"proxy_requests {vorher} -> {nachher}")


# ----------------------------------------------------------------- Monitoring
def check_monitoring() -> None:
    print("\nMonitoring")
    try:
        h = hole(DASHBOARD + "/api/health", timeout=5)
        pruefe("Dashboard-API", True, bool(h.get("ok")), f"host={h.get('host')}")
    except Exception as e:  # noqa: BLE001
        pruefe("Dashboard-API", True, False, f"{type(e).__name__}: {e}")
        return
    try:
        g = hole(DASHBOARD + "/api/gpu/live", timeout=15)
        gpus = g.get("gpus", [])
        pruefe("GPUs sichtbar", True, len(gpus) >= 1,
               ", ".join(f"GPU{x['gpu_id']} {x['mem_used_mib']}/{x['mem_total_mib']} MiB"
                         for x in gpus))
    except Exception as e:  # noqa: BLE001
        pruefe("GPUs sichtbar", True, False, f"{type(e).__name__}: {e}")

    # Schreibt der Log-Parser? Wenn der Inferenz-Check oben lief, muss in den
    # letzten Minuten eine Zeile entstanden sein.
    try:
        r = hole(DASHBOARD + "/api/requests?limit=1", timeout=10)
        items = r.get("items", [])
        letzte = items[0]["timestamp"] if items else "keine"
        pruefe("Request-Log wird geschrieben", False, bool(items),
               f"jüngste Zeile: {letzte}")
    except Exception as e:  # noqa: BLE001
        pruefe("Request-Log wird geschrieben", False, False, f"{type(e).__name__}: {e}")


def check_aufraeumen() -> None:
    """Läuft die Aufräumung wirklich — und liefert `docker logs` noch Aktuelles?

    Beide Prüfungen gibt es, weil beide Fehler am 20.08.2026 gefunden wurden und
    beide dieselbe Signatur haben: **im Erfolgsfall sehen sie aus wie im
    Fehlerfall.** Die Cron-Retention lief 146 Tage lang nie, während der Dienst
    „aktiv" meldete; `docker logs` lieferte plausible Zeilen, die nur sechs
    Wochen alt waren. Ohne diese zwei Zeilen fällt so etwas erst auf, wenn man
    die Daten braucht — also im Störungsfall.
    """
    print("\nAufräumen und Logs")

    # 1) Retention: der letzte Lauf muss jünger als 25 h sein (Timer läuft täglich).
    # `DB` kommt aus config.toml. Vorher stand hier ein Pfad, der aus einem
    # hartkodierten Repo-Ort gebaut wurde — auf ti11 liegt das Repo eine Ebene
    # tiefer, die Datei wurde nicht gefunden, und genau dieser Wächter meldete
    # „monitor.db fehlt", während die Retention tatsächlich lief (fA-439).
    # Ein Wächter, der Fehlalarm gibt, kann den echten Ausfall nicht mehr davon
    # unterscheiden — und der echte Ausfall ist der, den es hier zu fangen gilt.
    db = str(DB)
    if not os.path.exists(db):
        pruefe("Retention lief in den letzten 25 h", False, False,
               f"{db} fehlt — db_path in monitoring/config.toml prüfen")
    else:
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            row = conn.execute(
                "SELECT ts, ok FROM maintenance_log WHERE job='retention' "
                "ORDER BY ts DESC LIMIT 1").fetchone()
            conn.close()
            if row is None:
                pruefe("Retention lief in den letzten 25 h", False, False,
                       "kein Lauf verzeichnet — Timer aktiv? "
                       "systemctl list-timers ollama-monitor-retention")
            else:
                ts, ok = row
                alter_h = (datetime.now(timezone.utc)
                           - datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
                           .replace(tzinfo=timezone.utc)).total_seconds() / 3600
                pruefe("Retention lief in den letzten 25 h", False,
                       alter_h < 25 and ok == 1,
                       f"letzter Lauf {ts} (vor {alter_h:.1f} h), ok={ok}")
        except Exception as e:  # noqa: BLE001
            pruefe("Retention lief in den letzten 25 h", False, False,
                   f"{type(e).__name__}: {e}")

    # 2) docker logs: liefert der Leser noch frische Zeilen?
    # Ein NUL-Block in der json.log (unsauberer Abbruch) lässt den Docker-Leser
    # genau dort stehenbleiben — die Rohdatei wächst weiter, `docker logs` bleibt
    # aber für immer in der Vergangenheit. Auf ti-30 lag das Loch bei 0,9 % der
    # Datei, damit waren 99 % des Logs unerreichbar.
    if shutil.which("docker") is None:
        return
    rc, out = sh("docker", "logs", "--tail", "1", OLLAMA_CONTAINER, timeout=60)
    # Format der Ollama-Zeilen: "[GIN] 2026/08/20 - 10:52:49 | …" — zwischen
    # Datum und Uhrzeit steht " - ", nicht nur "T" oder ein Leerzeichen.
    treffer = re.search(
        r"(\d{4})[-/](\d{2})[-/](\d{2})(?:T|\s+-\s+|\s+)(\d{2}):(\d{2}):(\d{2})", out)
    if not treffer:
        pruefe("Container-Log wird geschrieben", False, False,
               "keine Zeitmarke in der letzten Zeile")
        return
    j, mo, t, st, mi, s = (int(x) for x in treffer.groups())
    letzte = datetime(j, mo, t, st, mi, s, tzinfo=timezone.utc)
    alter_h = (datetime.now(timezone.utc) - letzte).total_seconds() / 3600
    frisch = pruefe("Container-Log wird geschrieben", False, alter_h < 24,
                    f"jüngste Zeile {letzte:%Y-%m-%d %H:%M:%S} (vor {alter_h:.1f} h)")

    # Entscheidend: `--tail` springet ans DATEIENDE und läuft am Nullloch vorbei,
    # `--since` liest von vorn und bleibt darin stehen. Frische letzte Zeile bei
    # gleichzeitig leerem --since ist deshalb der Nachweis eines kaputten
    # Vorwärtslesers — genau der Zustand, in dem `docker logs` im Störungsfall
    # nichts mehr hergibt, ohne das je zu melden. Gemessen ti-30/ti11 20.08.2026.
    if not frisch:
        return
    rc, out2 = sh("docker", "logs", "--since", "10m", OLLAMA_CONTAINER, timeout=180)
    zeilen = len([z for z in out2.splitlines() if z.strip()])
    pruefe("docker logs von vorn lesbar", False, zeilen > 0,
           f"--since 10m liefert {zeilen} Zeilen"
           + ("" if zeilen else " trotz frischer letzter Zeile — NUL-Block in der "
                               "json.log, siehe monitoring/STANDARD.md"))


def check_platte() -> None:
    print("\nSystem")
    rc, out = sh("df", "--output=pcent,avail", "-h", str(REPO))
    zeile = out.splitlines()[-1].strip() if out else ""
    belegt = int(zeile.split("%")[0]) if "%" in zeile else 0
    pruefe("Plattenplatz unter 85 %", False, belegt < 85, zeile)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schnell", action="store_true",
                    help="ohne Inferenz-Test (löst keinen Modell-Load aus)")
    a = ap.parse_args()

    print(f"### Stack-Check TI-30 — {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    check_container()
    check_dienste()
    check_konfig()
    check_log_deckel()
    check_ollama(a.schnell)
    check_logproxy()
    check_monitoring()
    check_aufraeumen()
    check_platte()

    hart = [e for e in ERG if e["hart"] and not e["ok"]]
    weich = [e for e in ERG if not e["hart"] and not e["ok"]]
    print("\n" + "=" * 66)
    print(f"{sum(1 for e in ERG if e['ok'])}/{len(ERG)} im Soll | "
          f"gerissen: {len(hart)} | Hinweise: {len(weich)}")
    if hart:
        print("NICHT BETRIEBSBEREIT: " + ", ".join(e["name"] for e in hart))
        return 1
    if weich:
        print("betriebsbereit, mit Hinweisen: " + ", ".join(e["name"] for e in weich))
        return 2
    print("betriebsbereit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
