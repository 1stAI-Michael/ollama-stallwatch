#!/usr/bin/env python3
"""Probe fuer den begrenzten LAN-Lauscher des Monitors (fA-2224).

Geprueft wird die MECHANIK, nicht die Konfiguration: Quellbegrenzung
(`verify_request`) und Pfadbegrenzung (`routen`) laufen hier gegen eigene
Instanzen auf 127.0.0.1 mit Ephemeral-Port. Der laufende Dienst wird nicht
angefasst; am Ende steht ein getrennter Abschnitt BETRIEB, der ihn nur liest.

Jeder gepruefte Fall hat einen UMGEDREHTEN Kontrollfall mit bekannter Antwort:
nicht "Begrenzung aus, mal sehen", sondern dieselbe Anfrage, die ohne die
Begrenzung 200 liefern MUSS. Sonst beweist ein 404 nur, dass irgendetwas nicht
antwortet.

    python3 tests/probe_lan_lauscher.py

KONTROLLSCHALTER:

    PROBE_OHNE_BEGRENZUNG=1 python3 tests/probe_lan_lauscher.py

Dann laufen die Faelle gegen einen GEWOEHNLICHEN ThreadingHTTPServer, also
ohne Quell- und Pfadbegrenzung. Rot werden MUESSEN dann:

    1a  /api/requests auf dem begrenzten Lauscher -> 404
    1b  /            auf dem begrenzten Lauscher -> 404
    1c  /api/stalls/<id>/stack                   -> 404
    1d  HEAD /serve.py                           -> 404
    3   fremde Quelle wird abgewiesen

Bleibt dabei alles gruen, prueft die Probe nichts. Exit 0 = alles gruen,
1 = mindestens ein Fall rot, 2 = die Probe selbst kam nicht zum Messen.
"""

import os
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "monitoring" / "api"))
import serve  # noqa: E402

OHNE = os.environ.get("PROBE_OHNE_BEGRENZUNG") == "1"
ROUTEN = ("/api/gpu/live", "/api/stalls")

ergebnisse = []


def pruefe(name, soll, ist, anmerkung=""):
    ok = soll == ist
    ergebnisse.append((ok, name))
    zeichen = "OK  " if ok else "ROT "
    print(f"  {zeichen} {name}: soll={soll} ist={ist}"
          f"{('  — ' + anmerkung) if anmerkung else ''}")


def lauscher(netze, routen):
    """Begrenzte Instanz — oder, mit Kontrollschalter, eine gewoehnliche."""
    if OHNE:
        srv = serve.http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    else:
        srv = serve.BegrenzterServer(("127.0.0.1", 0), serve.Handler,
                                     serve.netze_aus(netze), routen)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def hole(port, pfad, methode="GET", timeout=20):
    """(status, fehler). status=None heisst: es kam keine HTTP-Antwort."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{pfad}", method=methode)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:                      # Verbindung zu, kein HTTP
        return None, f"{type(e).__name__}: {e}"


def main():
    print(f"Probe LAN-Lauscher (fA-2224) — Begrenzung "
          f"{'AUS (Kontrollschalter)' if OHNE else 'AN'}\n")

    # ---------------- 1. Pfadbegrenzung ----------------
    print("1) Pfadbegrenzung: nur die zwei freigegebenen Pfade")
    srv, port = lauscher(["127.0.0.1"], ROUTEN)
    try:
        s, f = hole(port, "/api/stalls?limit=1")
        pruefe("freigegeben /api/stalls", 200, s, f)
        s, f = hole(port, "/api/gpu/live")
        pruefe("freigegeben /api/gpu/live", 200, s, f)
        s, f = hole(port, "/api/requests?limit=1")
        pruefe("1a gesperrt /api/requests", 404, s, f)
        s, f = hole(port, "/")
        pruefe("1b gesperrt / (Dashboard)", 404, s, f)
        s, f = hole(port, "/api/stalls/1/stack")
        pruefe("1c gesperrt /api/stalls/<id>/stack", 404, s, f)
        s, f = hole(port, "/serve.py", methode="HEAD")
        pruefe("1d gesperrt HEAD /serve.py", 404, s, f)
        s, f = hole(port, "/api/health")
        pruefe("gesperrt /api/health", 404, s, f)
    finally:
        srv.shutdown()
        srv.server_close()

    # ------- 2. Kontrollfall UMGEDREHT: ohne Pfadliste MUSS es 200 sein -------
    print("\n2) Kontrollfall umgedreht: dieselbe Instanz ohne Pfadliste")
    print("   (liefert das hier kein 200, war das 404 oben wertlos)")
    if OHNE:
        srv = serve.http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    else:
        srv = serve.BegrenzterServer(("127.0.0.1", 0), serve.Handler,
                                     serve.netze_aus(["127.0.0.1"]), ())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        s, f = hole(port, "/api/requests?limit=1")
        pruefe("ohne Pfadliste /api/requests", 200, s, f)
        s, f = hole(port, "/api/health")
        pruefe("ohne Pfadliste /api/health", 200, s, f)
    finally:
        srv.shutdown()
        srv.server_close()

    # ---------------- 3. Quellbegrenzung ----------------
    print("\n3) Quellbegrenzung: Freigabe fuer 192.0.2.1, Anfrage von 127.0.0.1")
    srv, port = lauscher(["192.0.2.1"], ROUTEN)
    try:
        s, f = hole(port, "/api/stalls?limit=1", timeout=10)
        pruefe("fremde Quelle bekommt keine HTTP-Antwort", None, s,
               f or "es kam eine Antwort")
    finally:
        srv.shutdown()
        srv.server_close()

    # ------- 4. Kontrollfall UMGEDREHT: eigene Adresse freigegeben -------
    print("\n4) Kontrollfall umgedreht: 127.0.0.1 freigegeben")
    print("   (liefert das hier kein 200, war das Abweisen oben wertlos)")
    srv, port = lauscher(["127.0.0.1"], ROUTEN)
    try:
        s, f = hole(port, "/api/stalls?limit=1")
        pruefe("freigegebene Quelle /api/stalls", 200, s, f)
    finally:
        srv.shutdown()
        srv.server_close()

    # ---------------- 5. Leere Freigabeliste startet nicht ----------------
    print("\n5) Leere Freigabeliste: der Lauscher darf NICHT starten")
    sicher = (serve.LAN_HOST, serve.LAN_ALLOW, serve.LAN_ROUTES)
    try:
        serve.LAN_HOST, serve.LAN_ALLOW, serve.LAN_ROUTES = "127.0.0.1", [], ROUTEN
        pruefe("lan_allow=[] -> kein Lauscher", None, serve.lan_lauscher())
        serve.LAN_HOST, serve.LAN_ALLOW, serve.LAN_ROUTES = \
            "127.0.0.1", ["127.0.0.1"], ()
        pruefe("lan_routes=() -> kein Lauscher", None, serve.lan_lauscher())
    finally:
        serve.LAN_HOST, serve.LAN_ALLOW, serve.LAN_ROUTES = sicher

    # ---------------- 6. Betrieb: nur lesen ----------------
    print("\n6) Betrieb (laufender Dienst, nur gelesen)")
    if not serve.LAN_HOST:
        print("  --   kein lan_host konfiguriert, Abschnitt entfaellt")
    else:
        def extern(adresse, pfad, timeout=10):
            req = urllib.request.Request(f"http://{adresse}:{serve.PORT}{pfad}")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.status, ""
            except urllib.error.HTTPError as e:
                return e.code, ""
            except Exception as e:
                return None, f"{type(e).__name__}: {e}"

        s, f = extern(serve.HOST, "/api/health")
        pruefe(f"Tailnet {serve.HOST} /api/health unveraendert", 200, s, f)
        s, f = extern(serve.HOST, "/api/requests?limit=1")
        pruefe(f"Tailnet {serve.HOST} /api/requests unveraendert", 200, s, f)
        s, f = extern(serve.LAN_HOST, "/api/gpu/live", timeout=20)
        pruefe(f"LAN {serve.LAN_HOST} von DIESEM Host abgewiesen", None, s,
               f or "diese Maschine ist nicht freigegeben, trotzdem Antwort")

    rot = [n for ok, n in ergebnisse if not ok]
    print(f"\n{len(ergebnisse) - len(rot)}/{len(ergebnisse)} gruen")
    if rot:
        print("ROT: " + ", ".join(rot))
    if OHNE:
        print("\nKontrollschalter war AN — die oben im Kopf genannten Faelle "
              "MUSSTEN rot sein.")
    return 1 if rot else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:                       # die Probe selbst kam nicht durch
        print(f"PROBE GESCHEITERT: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
