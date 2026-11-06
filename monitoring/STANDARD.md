# Flotten-Standard: Logs und Monitoring-Aufbewahrung

Gilt für alle Hosts, die diesen Stack fahren — derzeit **ti-30** und **ti11**.
Festgelegt am 2026-08-20 nach Befunden auf beiden Maschinen (fA-434, fA-432).

Der Standard hat zwei Teile: einen **Deckel auf die Container-Logs** und eine
**gestufte Aufbewahrung** der Monitoring-Datenbank. Beide sind so gebaut, dass
ihr Ausfall auffällt — das ist kein Beiwerk, sondern der Kern. Beide Fehler, aus
denen dieser Standard entstanden ist, hatten dieselbe Signatur:

> **Sie sahen im Erfolgsfall genauso aus wie im Fehlerfall.**

Die Cron-Retention lief auf ti-30 **146 Tage lang kein einziges Mal**, während
`systemctl is-active cron` grün meldete und die Datei in `/etc/cron.d` ordentlich
dastand. Und `docker logs` lieferte plausible Zeilen — sie waren nur sechs Wochen
alt. Wer solche Zustände nicht aktiv prüft, bemerkt sie im Störungsfall.

---

## 1. Container-Logs: 100 MB Deckel je Container

**Vorgabe:** `max-size = 25m`, `max-file = 4` → höchstens **100 MB je Container**.

Vier Rotationsfenster statt eines großen: dieselbe Obergrenze, aber im
Störungsfall bleibt mehr Kontext erhalten, als wenn eine einzige volle Datei
umgeschlagen wird.

**Zwei Stellen, absichtlich doppelt:**

```jsonc
// /etc/docker/daemon.json — wirkt daemonweit, auch für Container ohne Compose
"log-driver": "json-file",
"log-opts": { "max-size": "25m", "max-file": "4" }
```

```yaml
# docker-compose.yml — dokumentiert die Absicht im Git und reist mit dem Stack
x-logging: &logdefaults
  driver: json-file
  options:
    max-size: "25m"
    max-file: "4"
```

`sudo systemctl reload docker` übernimmt die Daemon-Einstellung und **startet
keine Container neu** (auf ti11 und ti-30 gemessen: `docker ps` vorher/nachher
identisch). Sie greift aber erst, wenn ein Container **neu erzeugt** wird —
ein `restart` genügt nicht.

**Warum das nötig war:** `json-file` rotiert per Voreinstellung **gar nicht**.
Auf ti-30 war die Ollama-Logdatei auf **919 MB** gewachsen, auf ti11 auf 868 MB.
Rund **61 MB pro Tag**, getrieben von `OLLAMA_DEBUG=1` in Verbindung mit einem
Poller, der beide Hosts im Sekundentakt auf `/api/ps` abfragt
(`cobol-bench/app/metrics.py`, `POLL_INTERVAL_S = 1.0`). Hochgerechnet ~22 GB
im Jahr, ohne Obergrenze.

`OLLAMA_DEBUG=1` darf **nicht** abgeschaltet werden, um das zu dämpfen — der
Log-Parser des Monitorings hängt daran. Die Lösung ist Rotation, nicht weniger
Logging.

### 1a. Der NUL-Block: warum `docker logs` still in der Vergangenheit stehenbleibt

Am **2026-07-05 gegen 15:38** hat ein unsauberer Abbruch auf **beiden** Maschinen
einen Block aus NUL-Bytes in der `json.log` hinterlassen (ti-30 um 15:38:21,
ti11 um 15:38:22 — dasselbe Ereignis). Der Docker-Logleser läuft beim Lesen von
vorn in dieses Loch und **hört dort auf**.

Auf ti-30 lag das Loch bei Byte 8.662.820, also bei **0,9 %** der Datei: 99 % des
Logs waren für `docker logs` unerreichbar, während die Datei weiterwuchs. Der
Daemon sagt es, aber nur im eigenen Journal:

```
level=warning msg="Error decoding log file" error="invalid character '\x00' looking for beginning of value"
```

**Der Nachweis, gemessen — und feiner als zunächst gedacht:**

| Aufruf | Verhalten |
|---|---|
| `docker logs --tail 1 ollama` | springt ans **Dateiende**, läuft am Loch vorbei, liefert Aktuelles |
| `docker logs --since 10m ollama` | liest **von vorn**, bleibt im Loch stehen, liefert **0 Zeilen** |

Eine frische letzte Zeile bei gleichzeitig leerem `--since` ist damit der
Beweis. Wer nur `--tail` prüft, sieht das Problem **nie** — die Prüfung sähe im
Fehlerfall aus wie im Erfolgsfall. `tests/stack_check.py` prüft deshalb beides.

Loch suchen:

```bash
LP=$(docker inspect ollama --format '{{.LogPath}}')
sudo grep -abo -m1 $'\x00\x00\x00\x00' "$LP"
```

**Weg geht es nur mit einer neuen Logdatei**, also beim Neuerzeugen des
Containers (`docker compose up -d --force-recreate <dienst>`). Das ist ein
echter Dienst-Neustart und gehört auf einer Maschine im klinischen Betrieb
abgestimmt. Vorher sichern, falls der Inhalt gebraucht wird:

```bash
sudo tail -c 60000000 "$LP" | gzip -6 > monitoring/backup/ollama-json-log-$(date +%F).gz
```

Mit dem 100-MB-Deckel altert ein solcher Schaden künftig von selbst heraus,
statt für immer mitgeschleppt zu werden.

---

## 2. Monitoring-Datenbank: gestufte Aufbewahrung

**Vorgabe:**

| Stufe | Auflösung | Aufbewahrung |
|---|---|---|
| Rohdaten | 1 s (GPU) bzw. 10 s (Prozesse) | **24 h** |
| Stufe 2 | 5-s-Fenster | **7 Tage** |
| Stufe 3 | 60-s-Fenster | **360 Tage** |

Konfiguriert in `monitoring/config.toml`:

```toml
[retention]
raw_hours = 24
tiers = [[5, 7], [60, 360]]
events_days = 360
stalls_days = 360
vacuum_min_deleted = 100000
```

Ausgeführt von `monitoring/retention.py`, täglich um 03:17 über
`ollama-monitor-retention.timer`.

### Drei Regeln, die nicht verhandelbar sind

**a) Verdichten, nicht ausdünnen.** Pro Fenster entsteht eine Zeile mit
**Mittelwert UND Maximum**. Wer nur jede n-te Zeile behält, bekommt ungleiche
Lücken; wer nur Mittelwerte speichert, verliert genau das, was die Fragen
beantwortet. Konkret: der Beweis, dass Ollama ein Modell über beide Karten legt,
hängt an `MAX(vram_used_mib)` — GPU0 21.052 MiB **und gleichzeitig** GPU1 11.956
MiB am 09.08. Im Mittelwert wäre das nicht mehr zu sehen. Nach der ersten
Verdichtung wurde genau dieses Wertepaar gegengeprüft und war erhalten.

**b) Ereignisse werden nie verdichtet.** `ollama_requests` und `stall_events`
sind **Forensik, keine Statistik**: Modellverteilung, Client-IPs, wann ein
Modell verstummt ist. Sie werden nur gelöscht, und zwar erst nach 360 Tagen.
Sie kosten fast nichts — auf ti-30 rund 1.400 Zeilen am Tag.

**c) Ein Fenster kann nie feiner sein als seine Quelle.** Bei 10-s-Abtastung ist
eine 5-s-Stufe sinnlos. `retention.py` erkennt das an `poll_interval_sec` und
überspringt die Stufe selbstständig — für `system_metrics` (10 s) entfällt die
5-s-Stufe deshalb, für `gpu_metrics` (1 s) greift sie.

### Der 1-Sekunden-Takt darf nichts kosten

Bei 1 Hz einen `nvidia-smi`-Prozess je Abtastung zu starten wäre Verschwendung:
gemessen ~29 ms je Aufruf, also rund **3 % eines Kerns**, nur fürs Starten — auf
einer Maschine, die klinisch inferiert. Der Collector fährt deshalb unterhalb von
5 s **einen** dauerhaften `nvidia-smi --loop-ms` und liest dessen Ausgabe. Der
Kindprozess wird überwacht und mit Backoff neu gestartet; ohne das würde der
Collector still verstummen, während systemd „aktiv" meldet.

Ein **Heartbeat von 60 s** erzwingt je GPU mindestens eine Zeile pro Minute,
auch wenn sich nichts ändert. Damit ist jedes 60-s-Fenster der gröbsten Stufe
garantiert belegt, und eine ruhige Phase bleibt als ruhige Phase sichtbar statt
als Lücke.

### Ergebnis der Erstumstellung auf ti-30

| | vorher | nachher |
|---|---|---|
| Datenbank | 210 MB, **ohne Obergrenze** | **93 MB**, gedeckelt |
| `gpu_metrics` roh | 1.552.673 | 13.225 (24 h) |
| Rollups | — | 79.534 × 5 s + 302.926 × 60 s |
| `ollama_requests` | 210.903 | **210.903 (unverändert)** |
| Abdeckung | 146 Tage in einer Stufe | 24 h / 7 d / 360 d |

---

## 3. Regeln, die aus den Fehlern folgen

**Keine mehrzeiligen `cron.d`-Einträge. Nie.** Das crontab-Format kennt keine
Fortsetzungszeilen; jede Zeile ist ein eigener Eintrag. Ein mehrzeiliger Befehl
wird deshalb **nie an eine Shell übergeben**. Genau daran ist die alte Retention
gescheitert, und weil die Ausgabeumleitung Teil des nie ausgeführten Befehls war,
entstand auch keine Logdatei, die es hätte verraten können. Arbeit gehört in eine
**Skriptdatei**, ausgelöst von einem **systemd-Timer**.

*Die Umkehrung war übrigens der Beweis:* eine Umleitung wird von der Shell
**vor** dem Befehl aufgebaut. Hätte Cron die Zeile auch nur übergeben, um dann zu
scheitern, existierte die Logdatei. Sie existierte nicht — also wurde sie nie
übergeben.

**Jeder Wartungsjob protokolliert seinen eigenen Lauf.** `retention.py` schreibt
nach jedem Durchgang eine Zeile nach `maintenance_log` (Zeitpunkt, Erfolg, Dauer,
gelöschte Zeilen je Tabelle) — **auch wenn er scheitert**. Ohne das wäre ein
gescheiterter Lauf von einem nie gestarteten nicht zu unterscheiden.

**Eine WAL-Datenbank kopiert man mit `VACUUM INTO`, nicht mit `cp`.** Im
WAL-Modus steht der jüngste Stand im `-wal`-Beiblatt, nicht in der Hauptdatei.
Wer nur `monitor.db` kopiert, bekommt eine Datei, die sich öffnen lässt und beim
ersten Zugriff `no such table` sagt — gemessen von GX-10-Ops am 05.11.2026
(fA-1922), als sie aus einer Kopie eine Probe fahren wollten. Richtig ist

```bash
sqlite3 monitoring/monitor.db "VACUUM INTO '/pfad/kopie.db'"
# oder in Python: conn.execute("VACUUM INTO ?", (ziel,))
```

Die Sicherung in Abschnitt 4 nimmt deshalb `sqlite3.backup()` — dieselbe
Eigenschaft, derselbe Grund. Die Falle ist, dass `cp` **nicht scheitert**: die
Kopie kommt an, sie ist nur leer.

**Eine gestellte Zeile muss die neueste sein.** Wer eine Probe mit einem
künstlichen Datensatz füttert, der gegen `MAX(timestamp)` konkurriert, beweist
nichts: eine 400 s alte gestellte Zeile neben echten jungen verliert und die
Probe wird grün, obwohl nichts geprüft wurde (ebenfalls GX-10-Ops, 05.11.2026).

**Jede stille Fehlerquelle bekommt eine Prüfung in `tests/stack_check.py`.**
Derzeit drei:

* *Retention lief in den letzten 25 h* — liest `maintenance_log`.
* *docker logs von vorn lesbar* — vergleicht `--tail` gegen `--since`.
* *Log-Deckel je Container* — liest `HostConfig.LogConfig`, weil die Option
  gesetzt sein kann, ohne dass sie am laufenden Container greift.

**Ein Dienst, der immer laufen soll, bekommt `Restart=always` — nicht
`on-failure`.** Ein Sammler, der sich beendet, weil sein Container gerade nicht
da ist, endet mit **Exit 0**; `on-failure` holt ihn dann nie zurück. Gemessen
ti-30 20.08.2026: nach einem Neustart lief `ollama-log-parser` 123 ms, meldete
„container 'ollama' not present, exiting cleanly" und blieb **drei Stunden tot**,
während `systemctl` ihn als `enabled` führte. Die Request-Zeilen dieser drei
Stunden fehlen dauerhaft. Gegenprobe an einer Wegwerf-Unit: `Restart=always` mit
`/bin/sh -c 'exit 0'` ergab fünf Neustarts in sechs Sekunden — `on-failure`
hätte keinen einzigen gemacht.

**Kein Textmuster auf strukturierte Ausgabe.** Die erste Fassung der
Deckel-Prüfung klopfte die JSON-Zeile von `docker inspect` mit dem Glob
`*25m*4*` ab und meldete an fünf **korrekt konfigurierten** Containern einen
Fehler: Docker gibt `{"max-file":"4","max-size":"25m"}` aus, die 4 steht vor der
25m. Geprüft wurde damit die Reihenfolge der Felder, nicht ihr Inhalt. Wer
strukturierte Ausgabe hat, liest sie feldweise — `--format '{{index …}}'` in der
Shell, `json.loads` in Python.

**Ein Trockenlauf muss die Wirkung zeigen, nicht die Arbeit.** `--trocken` gab
anfangs stur `geloescht: 0` bei `gelesen: 455430` aus. ti11-Ops hat sich zu Recht
nicht darauf verlassen und stattdessen scharf gegen die Sicherungskopie gefahren
(fA-439). Nach unserem eigenen Maßstab war das eine Prüfung, die im Erfolgsfall
aussieht wie im Fehlerfall. Jetzt meldet die Vorschau, was gelöscht **würde**,
und zu wie vielen Fenstern verdichtet wird; gegengeprüft am 20.08. auf einer
Kopie — Vorschau und scharfer Lauf stimmten in jedem Schritt und in der Summe
(8.582 Zeilen) überein.

**Eine Prüfung darf nicht die Eigenheiten EINES Hosts als Sollzustand
behaupten.** Auf ti11 rissen 8 von 25 Prüfungen, alle acht wegen ti-30-Annahmen
im Skript — bei nachweislich korrekter Konfiguration des laufenden Containers.
Die Meldung „NICHT BETRIEBSBEREIT: OLLAMA_CONTEXT_LENGTH = 131072" las sich wie
die Aufforderung, an einer gesunden Produktivkonfiguration zu drehen. **Ein
Check, der zum Eingriff in einen gesunden Zustand auffordert, ist schädlicher
als gar kein Check.** Konsequenz: Pfade kommen aus `config.toml`, Container- und
Modellnamen aus dessen optionalem `[stack_check]`-Block, und Sollwerte werden am
**laufenden Container** gemessen (`docker exec … env`) statt aus der
Compose-Datei gelesen — die Datei ist die Absicht, der Prozess ist der Betrieb.
Nebenwirkung, die den Aufwand allein schon trägt: `1` und `true` gelten für
Ollama-Schalter als gleichwertig, weil sie es sind.

**Zeitvergleiche in SQL nie mit `datetime('now', …)`.** Gespeichert wird
`2026-08-20T10:45:41Z`, `datetime()` liefert `2026-08-20 10:45:41`. Verglichen
wird als **Text**, und `T` (0x54) ist größer als das Leerzeichen (0x20) — dadurch
gilt jede Zeile desselben Tages als größer als jede Schranke desselben Tages,
unabhängig von der Uhrzeit. Beim Bau von `retention.py` gemessen: dieselbe
Abfrage lieferte für 30 s, 60 s und 300 s identisch 5.889 Zeilen statt 38 / 77 /
113. In einer Löschroutine ist das die gefährlichste Sorte Fehler. Richtig ist
`strftime('%Y-%m-%dT%H:%M:%SZ','now',…)` — oder die Schranke in Python bauen, wie
`report.py` und `api/serve.py` es tun.

---

## 4. Einen Host umstellen

```bash
# 1. Sichern — vor dem ersten Lauf, nicht danach.
sudo python3 -c "
import sqlite3
s=sqlite3.connect('file:monitoring/monitor.db?mode=ro',uri=True)
d=sqlite3.connect('monitoring/backup/monitor-$(date +%F).db'); s.backup(d)"
gzip -6 monitoring/backup/monitor-*.db

# 2. Sicherung gegenlesen — eine ungeprüfte Sicherung ist keine.
#    PRAGMA integrity_check muss 'ok' liefern, Zeilenzahlen müssen passen.

# 3. Konfiguration ergänzen ([retention]-Block, gpu poll_interval_sec = 1)

# 4. Ausrollen (idempotent; entfernt den alten Cron-Eintrag mit)
sudo ./monitoring/install.sh

# 5. Trockenlauf ansehen, DANN scharf schalten
sudo python3 monitoring/retention.py --trocken
sudo systemctl start ollama-monitor-retention.service

# 6. Nachweisen
sudo python3 monitoring/retention.py --zeigen
python3 tests/stack_check.py --schnell
```

Für den Log-Deckel zusätzlich `daemon.json` ergänzen und `systemctl reload
docker`. Der Deckel greift erst beim Neuerzeugen eines Containers — auf einer
Maschine im klinischen Betrieb ist das ein abgestimmter Termin, keine
Handbewegung.

**Nach `docker compose down` kommen die Container nicht von allein zurück.**
`restart: unless-stopped` greift nur für Container, die es noch gibt — `down`
entfernt sie. Gemessen ti-30 20.08.2026: nach dem Wartungsfenster stand der Stack
drei Stunden, weil das Hochholen ein Handschritt war, den niemand ausführte.
Wer `down` fährt, plant das Hochfahren als eigenen, terminierten Schritt ein.

### Stand ti11 (erledigt 20.08.2026)

ti11 ist angeglichen: `25m × 4` in `daemon.json` und allen neun Compose-Diensten,
Cron-Eintrag entfernt, Timer aktiv, gestufte Aufbewahrung übernommen, `[gpu]` auf
1 Hz. Datenbank 61,3 MB → **27,6 MB**, `ollama_requests` und `stall_events`
unverändert. Split-Beweis gegengeprüft: im 60-s-Fenster steht Mittelwert 17.686
MiB neben Maximum **32.400** MiB — die Spitze überlebt die Verdichtung.

**Gegenprobe V100:** `nvidia-smi --loop-ms` läuft dort sauber; der Einzelaufruf
kostet im Median **27 ms** (ti-30: ~29 ms auf 3090 Ti / 5060 Ti), also ~2,7 %
eines Kerns bei 1 Hz. `STREAM_THRESHOLD_SEC` musste nicht greifen.

Drei Korrekturen von ti11-Ops an der ersten Fassung dieses Standards, hier
eingearbeitet:

* Die **2,49-GB-Datenbank ist `vram_metrics.db` von `cobol-bench` auf
  ti-nas-06** (fA-432), nicht ti11s `monitor.db` — die war 61 MB.
* **ti11 tastete nicht mit 1 Hz ab**, sondern mit 10 s (~479 Messpunkte/h). Der
  1-Hz-Poller ist `cobol-bench` von außen, nicht der lokale Collector.
* Der Cron-Eintrag hatte dort einen **zweiten** Defekt: einen falschen DB-Pfad
  (`monitoring/monitor.db` statt `monitoring/monitoring/monitor.db`, weil das
  Repo eine Ebene tiefer liegt). Selbst mit repariertem crontab-Format hätte der
  Job eine leere Datenbank angelegt und 0 Zeilen gelöscht — **erfolgreich, und
  wirkungslos.** Derselbe fehlende Pfad legte auch den Retention-Wächter in
  `stack_check.py` still lahm; beides ist mit dem `config.toml`-Pfad behoben.

---

## 5. Offen, bewusst nicht mit erledigt

* **Der 1-Hz-Poller selbst.** `cobol-bench/app/metrics.py` fragt beide Hosts im
  Sekundentakt ab; laut fA-432 soll das auf 30 s fallen. Das entschärft das
  Wachstum, ersetzt aber weder Rotation noch Aufbewahrung.
* **Eigentum im Repo.** `monitoring/collectors/*.py` und `api/serve.py` gehören
  `root`, `db.py` dagegen `ollama_admin` — und genau die importiert jeder der
  fünf root-Dienste. Die root-Eigentümerschaft der Collectoren schützt dadurch
  nichts. Praktisch entschärft, weil `ollama_admin` ohnehin passwortloses sudo
  hat; sauber ist es nicht. Nebenwirkung: `git pull` kann diese Dateien als
  `ollama_admin` nicht aktualisieren. **Rechte sind Tabu-Klasse — gemeldet,
  nicht geändert.**

  **Konkrete Falle, gemessen 20.08.2026: `git checkout` wechselt den Branch
  trotzdem.** Er kann die root-eigenen Collector-Dateien nicht zurücksetzen,
  bricht deswegen aber nicht ab — Ergebnis war ein halb umgestellter
  Arbeitsbaum: `HEAD` auf dem Zielbranch, vier Dateien von der Platte
  verschwunden (darunter `retention.py`, auf die der Timer zeigt), zwei
  root-eigene noch auf dem alten Stand. Nichts ging verloren, weil alles
  festgeschrieben war, aber die Dienste liefen minutenlang über Dateien, die
  es nicht mehr gab. **Auf dieser Maschine den Branch nicht wechseln, solange
  die Eigentumslage so ist.** Wer `master` nachziehen will, bewegt die Zeiger
  statt den Arbeitsbaum:

  ```bash
  git push origin <branch>:master     # Remote vorspulen
  git update-ref refs/heads/master <sha>   # lokal nachziehen, kein Checkout
  ```
* **Das bestehende NUL-Loch auf ti-30.** Verschwindet erst beim Neuerzeugen des
  Containers. Bis dahin gilt: fürs Log der Rohzugriff auf
  `docker inspect ollama --format '{{.LogPath}}'`, nicht `docker logs`.
