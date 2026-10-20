#!/usr/bin/env python3
"""Klinischer Referenz-Satz — macht "keine Veränderung an den Ergebnissen" prüfbar.

Das Eval-Gate prüft Mechanik: bricht ein Fehlschlag sichtbar ab. Es prüft NICHT,
ob dieselbe Frage dieselbe Antwort bekommt. Genau das ist auf dieser Maschine
aber die Vorgabe — und genau das ändert sich lautlos, wenn jemand die Engine
hebt, einen Modell-Tag neu zieht oder die Kontextlänge verstellt: kein Fehler,
keine Warnung, nur andere Antworten.

Dieses Skript legt einen Referenz-Satz an und vergleicht später dagegen.

    python3 tests/clinical_baseline.py --rauschboden      # ist der Satz reproduzierbar?
    python3 tests/clinical_baseline.py --anlegen          # Referenz schreiben
    python3 tests/clinical_baseline.py --vergleichen      # gegen Referenz pruefen

Die Prompts sind bewusst **synthetisch und ohne Personenbezug** — es geht um
Antwort-Stabilität, nicht um klinische Richtigkeit. Es gehören keine echten
Patientendaten in eine Datei, die im Repo landet.

WICHTIG — warum die Reihenfolge Teil der Referenz ist (gemessen 2026-08-09):
Das Modell ist deterministisch, aber die Antwort haengt am Prefix-Cache des
Slots. Derselbe Prompt liefert reproduzierbar ZWEI verschiedene Antworten, je
nachdem was unmittelbar davor im Slot lag:

    A nach einem anderen Prompt  -> 298 Zeichen, sha 48566d… (3 von 3 Durchgaengen)
    A direkt nach sich selbst    -> 343 Zeichen, sha 29019… (2 von 2 Durchgaengen)

Beide Werte sind stabil, es ist kein Rauschen. Konsequenz: der Satz laeuft in
**fester Reihenfolge** und enthaelt **keinen Prompt doppelt**. Wer einzelne
Eintraege herausgreift und wiederholt, misst den anderen Zweig und haelt ihn
faelschlich fuer eine Veraenderung.

Exit 0 = deckungsgleich · 1 = Abweichung gefunden · 2 = Referenz fehlt/unbrauchbar.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request

OLLAMA = os.environ.get("OLLAMA_BASE", "http://127.0.0.1:11434")
MODELL = os.environ.get("BASELINE_MODEL", "alibayram/medgemma:27b")
REFERENZ = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "clinical_baseline_results.json")

# Feste Parameter. Jede Änderung hier entwertet die Referenz — dann neu anlegen.
OPTIONEN = {"temperature": 0.0, "seed": 42, "num_predict": 200, "top_k": 1, "top_p": 1.0}

PROMPTS: list[tuple[str, str]] = [
    ("terminologie-1", "Erklaere den Unterschied zwischen Praevalenz und Inzidenz in zwei Saetzen."),
    ("terminologie-2", "Was bedeutet die Abkuerzung CRP in der Labordiagnostik? Antworte in einem Satz."),
    ("struktur-1", "Gliedere einen Arztbrief in seine ueblichen Abschnitte. Nur die Ueberschriften, als Liste."),
    ("struktur-2", "Nenne die fuenf Vitalparameter, die bei einer Aufnahme routinemaessig erhoben werden. Nur die Liste."),
    ("klassifikation-1", "Zu welchem ICD-10-Kapitel gehoeren Erkrankungen des Kreislaufsystems? Nenne Kapitel und Bereich."),
    ("dosis-1", "Erklaere, was 'BID' und 'TID' auf einem Medikationsplan bedeuten. Ein Satz je Abkuerzung."),
    ("abgrenzung-1", "Worin unterscheiden sich Sensitivitaet und Spezifitaet eines Tests? Zwei Saetze."),
    ("vignette-1", "Fasse zusammen, welche Angaben eine Anamnese mindestens enthalten soll. Als kurze Liste."),
    ("negativ-1", "Nenne drei Gruende, warum ein Laborwert falsch-positiv ausfallen kann. Als Liste."),
    ("format-1", "Antworte ausschliesslich mit dem Wort: bereit"),
    ("laenge-1", "Beschreibe in genau drei Saetzen, wozu eine Verlaufsdokumentation dient."),
    ("sprache-1", "Uebersetze den Begriff 'shortness of breath' ins Deutsche. Nur der Fachbegriff."),
]


def chat(prompt: str, timeout: float = 900.0) -> tuple[str, float, dict]:
    body = {"model": MODELL, "stream": False, "think": False, "truncate": False,
            "messages": [{"role": "user", "content": prompt}], "options": OPTIONEN}
    req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    text = ((d.get("message") or {}).get("content") or "").strip()
    return text, time.time() - t0, d


def sig(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def umgebung() -> dict:
    """Alles, was die Antworten verändern kann — die Referenz ist ohne das wertlos."""
    umf: dict = {"modell": MODELL, "optionen": OPTIONEN, "ollama_base": OLLAMA}
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=10) as r:
            for m in json.loads(r.read().decode()).get("models", []):
                if m["name"] == MODELL:
                    umf["digest"] = m.get("digest")
                    umf["groesse"] = m.get("size")
                    umf["geaendert"] = m.get("modified_at")
    except Exception as e:  # noqa: BLE001
        umf["digest_fehler"] = f"{type(e).__name__}: {e}"
    try:
        with urllib.request.urlopen(OLLAMA + "/api/version", timeout=10) as r:
            umf["ollama_version"] = json.loads(r.read().decode()).get("version")
    except Exception:  # noqa: BLE001
        pass
    return umf


def warmlauf() -> None:
    """Ohne Warmlauf misst der erste Durchgang den Modell-Ladevorgang mit."""
    print("  warmlaufen …", end="", flush=True)
    t0 = time.time()
    chat("Antworte mit: ok")
    print(f" {time.time() - t0:.1f}s")


def stabilisieren() -> None:
    """Ein Durchgang unbewertet, damit der Slot-Zustand VOR der Messung festliegt.

    Michael, 20.10.2026, nach dem Umstieg auf ollama 0.40.6: dort haengt
    `terminologie-1` vom Slot-Zustand ab — warm war `--vergleichen` dreimal 12/12,
    nach einem Neuanlegen des Behaelters meldete derselbe Eintrag 353 -> 310 Zeichen,
    und zwar in drei von drei Laeufen zeichengleich. Also zwei feste Zustaende, kein
    Rauschen. Auf 0.19.0 trat das nicht auf.

    Der Rauschboden zeigt den Ausweg: ab dem ZWEITEN Durchgang ist der Satz identisch
    (Durchgaenge 2-5 deckungsgleich). Ein unbewerteter Durchgang vorweg laesst warmen
    und kalten Fall im gleichen Zustand starten.

    Prompts, Reihenfolge, Modell und Sampling-Optionen sind UNVERAENDERT. Festgelegt
    wird nur der Zustand davor. Eine mit dieser Fassung angelegte Referenz gilt
    deshalb auch nur fuer diese Fassung — nach der Umstellung neu anlegen.

    KONTROLLFALL (fA-648): PROBE_OHNE_STABILISIERUNG=1 schaltet den Durchgang ab.
    Dann MUSS `--vergleichen` nach einem Neuanlegen des Behaelters bei
    `terminologie-1` rot werden. Bleibt es gruen, prueft die Stabilisierung nichts.
    """
    if os.environ.get("PROBE_OHNE_STABILISIERUNG") == "1":
        print("  [KONTROLLFALL] Stabilisierungsdurchgang abgeschaltet")
        return
    print("  stabilisieren (ein Durchgang unbewertet) …", end="", flush=True)
    t0 = time.time()
    for _, prompt in PROMPTS:
        chat(prompt)
    print(f" {time.time() - t0:.1f}s")


# ---------------------------------------------------------------- Rauschboden
def rauschboden(n: int) -> int:
    """Liefert der SATZ bei Wiederholung dieselben Antworten?

    Bewusst der ganze Satz in fester Reihenfolge und nicht ein Prompt n-mal:
    Prompt-Wiederholung misst den Prefix-Cache-Zweig (s. Modul-Docstring) und
    wuerde Rauschen behaupten, wo Determinismus herrscht. Ohne diese Zahl ist
    die Referenz wertlos — schwankt der Satz schon ohne Aenderung, sagt eine
    spaetere Abweichung nichts."""
    print(f"### Rauschboden — Satz {n}x in fester Reihenfolge, {MODELL}\n")
    warmlauf()
    stabilisieren()
    durchgaenge: list[list[str]] = []
    for i in range(n):
        hashes = []
        for name, prompt in PROMPTS:
            t, _, _ = chat(prompt)
            hashes.append(sig(t))
        durchgaenge.append(hashes)
        print(f"  Durchgang {i+1}: {' '.join(h[:6] for h in hashes)}")
    abweichend = [PROMPTS[j][0] for j in range(len(PROMPTS))
                  if len({d[j] for d in durchgaenge}) > 1]
    print(f"\n  instabile Eintraege: {len(abweichend)} von {len(PROMPTS)}")
    if not abweichend:
        print("  => REPRODUZIERBAR. Jede spaetere Abweichung hat eine Ursache")
        print("     ausserhalb des Modells — Engine, Modell-Tag, Kontextlaenge.")
        return 0
    print("  => INSTABIL bei: " + ", ".join(abweichend))
    print("     Diese Eintraege aus der Referenz nehmen oder auf Laenge/Begriffe")
    print("     pruefen statt auf Gleichheit.")
    return 1


# -------------------------------------------------------------------- Anlegen
def anlegen() -> int:
    print(f"### Referenz-Satz anlegen — {len(PROMPTS)} Prompts, {MODELL}\n")
    umf = umgebung()
    print(f"  Modell-Digest: {umf.get('digest', '?')[:20]}…  Ollama {umf.get('ollama_version', '?')}")
    warmlauf()
    stabilisieren()
    eintraege = []
    for name, prompt in PROMPTS:
        t, w, d = chat(prompt)
        eintraege.append({"name": name, "prompt": prompt, "antwort": t, "sha": sig(t),
                          "zeichen": len(t), "eval_count": d.get("eval_count"),
                          "sekunden": round(w, 2)})
        print(f"  {name:<16} {w:5.1f}s  {len(t):4d} Zeichen  sha={sig(t)}")
    daten = {"erstellt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "umgebung": umf,
             "eintraege": eintraege}
    with open(REFERENZ, "w", encoding="utf-8") as f:
        json.dump(daten, f, indent=2, ensure_ascii=False)
    print(f"\n  geschrieben: {REFERENZ}")
    return 0


# --------------------------------------------------------------- Vergleichen
def vergleichen() -> int:
    if not os.path.exists(REFERENZ):
        print(f"Referenz fehlt: {REFERENZ} — erst --anlegen", file=sys.stderr)
        return 2
    alt = json.load(open(REFERENZ, encoding="utf-8"))
    print(f"### Vergleich gegen Referenz vom {alt['erstellt']}\n")

    # Zuerst die Umgebung: ein anderer Digest erklaert jede Abweichung sofort.
    neu_umf, alt_umf = umgebung(), alt["umgebung"]
    for schluessel in ("digest", "ollama_version", "modell"):
        a, n = alt_umf.get(schluessel), neu_umf.get(schluessel)
        if a != n:
            print(f"  [UMGEBUNG] {schluessel}: {a} -> {n}")
    if alt_umf.get("optionen") != neu_umf.get("optionen"):
        print("  [UMGEBUNG] Sampling-Optionen weichen ab — Vergleich nicht aussagekraeftig")

    warmlauf()
    stabilisieren()
    abweichungen = []
    for e in alt["eintraege"]:
        t, w, _ = chat(e["prompt"])
        gleich = sig(t) == e["sha"]
        print(f"  {e['name']:<16} {'gleich' if gleich else 'ABWEICHUNG'}"
              f"  {e['zeichen']:4d} -> {len(t):4d} Zeichen")
        if not gleich:
            abweichungen.append({"name": e["name"], "vorher": e["antwort"], "jetzt": t})
    print("\n" + "=" * 66)
    if not abweichungen:
        print(f"{len(alt['eintraege'])}/{len(alt['eintraege'])} deckungsgleich — "
              "keine Veraenderung an den Ergebnissen.")
        return 0
    print(f"{len(abweichungen)} von {len(alt['eintraege'])} Antworten haben sich geaendert:")
    for a in abweichungen:
        print(f"\n--- {a['name']} ---\n  vorher: {a['vorher'][:300]}\n  jetzt : {a['jetzt'][:300]}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rauschboden", type=int, nargs="?", const=5, default=None,
                    metavar="N", help="N-mal denselben Prompt fahren (Default 5)")
    ap.add_argument("--anlegen", action="store_true")
    ap.add_argument("--vergleichen", action="store_true")
    a = ap.parse_args()
    if a.rauschboden is not None:
        return rauschboden(a.rauschboden)
    if a.anlegen:
        return anlegen()
    if a.vergleichen:
        return vergleichen()
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
