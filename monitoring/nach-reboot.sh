#!/usr/bin/env bash
# Nach dem Neustart ausführen — bringt den Stack hoch und prüft ihn nach.
#
# Vorgeschichte: Am 2026-08-20 stand ein Kernelwechsel an (6.8.0-136 -> -138),
# und derselbe Termin sollte zwei weitere Dinge miterledigen:
#   * das NUL-Loch in der Ollama-Logdatei vom 05.07. (macht `docker logs`
#     von vorn unlesbar — verschwindet nur mit einer NEUEN Logdatei)
#   * den 100-MB-Log-Deckel scharf schalten (greift nur bei NEU ERZEUGTEN
#     Containern, nicht bei einem blossen Neustart)
#
# Deshalb wurden die Container vorher mit `docker compose down` sauber
# entfernt. Sie kommen also NICHT von allein zurueck — dieses Skript holt sie.
#
#   sudo bash /srv/Container/monitoring/nach-reboot.sh
#
# Exit 0 = alles im Soll · 1 = mindestens eine harte Pruefung gerissen.

set -uo pipefail
cd /srv/Container || exit 1

VOR=monitoring/backup/vor-reboot-2026-08-20.txt
FEHLER=0
ok()   { printf '  [OK  ] %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*"; FEHLER=$((FEHLER+1)); }
warn() { printf '  [WARN] %s\n' "$*"; }

echo "### Nach-Neustart-Pruefung — $(date -u '+%Y-%m-%d %H:%M:%S UTC')"

echo
echo "Kernel"
LAEUFT=$(uname -r)
if [ "$LAEUFT" = "6.8.0-136-generic" ]; then
  fail "laeuft weiterhin $LAEUFT — der neue Kernel wurde NICHT geladen"
else
  ok "laeuft $LAEUFT (vorher 6.8.0-136-generic)"
fi
[ -f /var/run/reboot-required ] && warn "reboot-required steht erneut" || ok "keine reboot-required-Markierung"

echo
echo "Stack starten"
# Nicht `docker compose up -d`: das zieht einen VORHANDENEN Container ohne
# Port-Mapping wieder hoch (auf gx10-01 gemessen, fA-507 K2012: `Ports: []` —
# ein Dienst, der auf gruen steht und nicht erreichbar ist). Das Nachstart-
# Skript unterscheidet die Faelle und nimmt dann `--force-recreate`.
bash monitoring/compose-nachstart.sh 2>&1 | sed 's/^/    /'
echo "  warte auf Ollama ..."
for i in $(seq 1 60); do
  V=$(curl -s -m 3 http://127.0.0.1:11434/api/version 2>/dev/null) && [ -n "$V" ] && break
  sleep 2
done
[ -n "${V:-}" ] && ok "Ollama antwortet: $V" || fail "Ollama antwortet nach 120 s nicht"

echo
echo "Container"
# tei-embed steht hier nicht mehr: seit 20.10.2026 abgeschaltet (fA-1311/fA-1575).
# Compose startet ihn nicht mehr (Profil "aus"), also darf er nach einem Neustart
# auch nicht laufen — ihn hier zu verlangen, haette bei jedem Reboot ein falsches
# Rot erzeugt. Zurueck: `docker compose --profile aus up -d tei-embed` und diese
# beiden Listen wieder ergaenzen.
for c in ollama openwebui tei-rerank rerank-adapter; do
  S=$(docker inspect "$c" --format '{{.State.Status}}' 2>/dev/null)
  [ "$S" = "running" ] && ok "$c laeuft" || fail "$c: Status '$S'"
done

echo
echo "Log-Deckel — der eigentliche Zweck des Termins"
# Feldweise abfragen statt die JSON-Zeile mit einem Muster abklopfen.
# Die erste Fassung prüfte mit `case "$CFG" in *25m*4*)` und meldete am 20.08.
# an fünf KORREKT konfigurierten Containern einen Fehler: Docker gibt
# {"max-file":"4","max-size":"25m"} aus — die 4 steht vor der 25m, das Muster
# verlangte die andere Reihenfolge. Ein Textmuster über strukturierte Ausgabe
# prüft die Formatierung, nicht den Wert.
for c in ollama openwebui tei-rerank rerank-adapter; do
  GROESSE=$(docker inspect "$c" --format '{{index .HostConfig.LogConfig.Config "max-size"}}' 2>/dev/null)
  DATEIEN=$(docker inspect "$c" --format '{{index .HostConfig.LogConfig.Config "max-file"}}' 2>/dev/null)
  if [ "$GROESSE" = "25m" ] && [ "$DATEIEN" = "4" ]; then
    ok "$c: max-size=$GROESSE max-file=$DATEIEN"
  else
    fail "$c: Deckel NICHT aktiv -> max-size='${GROESSE:-fehlt}' max-file='${DATEIEN:-fehlt}'"
  fi
done

echo
echo "docker logs wieder von vorn lesbar?"
# Der Nachweis ist die KOMBINATION: --tail springt ans Dateiende und lief auch
# vorher schon, nur --since liest von vorn und blieb im NUL-Loch stehen.
sleep 20   # ein paar Zeilen entstehen lassen
LETZTE=$(docker logs --tail 1 ollama 2>&1 | head -1)
ZEILEN=$(docker logs --since 10m ollama 2>&1 | grep -c . )
if [ "$ZEILEN" -gt 0 ]; then
  ok "--since 10m liefert $ZEILEN Zeilen — NUL-Loch ist weg"
else
  fail "--since 10m liefert 0 Zeilen, letzte Zeile: ${LETZTE:0:70}"
fi
LP=$(docker inspect ollama --format '{{.LogPath}}')
echo "  neue Logdatei: $(stat -c%s "$LP" 2>/dev/null) Byte"

echo
echo "Monitoring-Dienste"
for u in ollama-gpu-logger ollama-log-parser ollama-process-logger \
         ollama-stall-detector ollama-dashboard; do
  A=$(systemctl is-active "$u")
  [ "$A" = "active" ] && ok "$u" || fail "$u: $A"
done
A=$(systemctl is-active ollama-monitor-retention.timer)
[ "$A" = "active" ] && ok "ollama-monitor-retention.timer" || fail "Retention-Timer: $A"

echo
echo "Sammelt das Monitoring wieder?"
sleep 15
N=$(python3 - <<'PY'
import sqlite3
c=sqlite3.connect('file:/srv/Container/monitoring/monitor.db?mode=ro',uri=True)
print(c.execute("SELECT COUNT(*) FROM gpu_metrics WHERE timestamp > strftime('%Y-%m-%dT%H:%M:%SZ','now','-120 seconds')").fetchone()[0])
PY
)
[ "${N:-0}" -gt 0 ] && ok "gpu_metrics: $N neue Zeilen in 120 s" || fail "gpu_metrics: keine neuen Zeilen"

echo
echo "GPUs"
nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader | sed 's/^/  /' \
  || fail "nvidia-smi nicht ansprechbar"

echo
echo "=================================================================="
if [ "$FEHLER" -eq 0 ]; then
  echo "Technik im Soll. JETZT die inhaltliche Probe — sie ist die eigentliche:"
else
  echo "$FEHLER Pruefung(en) gerissen. Erst beheben, dann die inhaltliche Probe."
fi
cat <<'TXT'

    python3 tests/clinical_baseline.py --vergleichen

  Muss Exit 0 liefern (deckungsgleich mit der Referenz vom 09.08.).
  Weicht sie ab, hat der Kernelwechsel Antworten veraendert — das waere ein
  meldepflichtiger Befund, kein Schoenheitsfehler. Der Vergleichslauf von
  VOR dem Neustart liegt in:
      monitoring/backup/baseline-vor-reboot-2026-08-20.txt

  Danach zur Vollstaendigkeit:
    python3 tests/stack_check.py --schnell
    ~/.local/share/1stai-knowledge-zentral/kit/studio-kit/kit.sh status

  Zustand von vorher zum Gegenlesen: monitoring/backup/vor-reboot-2026-08-20.txt
TXT
exit $([ "$FEHLER" -eq 0 ] && echo 0 || echo 1)
