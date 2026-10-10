# Offen aus den Audits vom 06./07.10.2026

Sieben Audit-Durchläufe an zwei Tagen — zwei vor `acdd26e`, fünf danach.
Was behoben ist, steht in den Commit-Botschaften; **hier steht nur, was
offen ist**, damit es nicht im Sitzungsverlauf verschwindet.

Drei Zustände, und sie sind nicht dasselbe:

* **gemeldet** — ein Agent hat es behauptet, ich habe es *nicht*
  nachgestellt. Ein Agentenbericht ist ein Hinweis, kein Beweis.
* **nachgestellt** — ich habe es selbst gemessen, mit der Messung dabei.
* **entschieden** — angesehen und bewusst so gelassen, mit Grund.

Erledigtes wird hier gelöscht, nicht abgehakt: eine Liste voller
durchgestrichener Zeilen liest irgendwann niemand mehr.

---

## 1 — ~~Zusagen, die nichts absicherte~~ — alle sieben erledigt

**07.10.2026.** Die suiteweite Mutationsprobe hatte sieben Kernzusagen
gefunden, deren Durchsetzung man löschen kann, ohne dass eine von 181
Prozeduren rot wird. Alle sieben haben jetzt eigene Prüfungen, und
`test_guard_mutations.py` bricht für jede die Zeile, die sie durchsetzt
— 69 Einträge, Schuldenliste **leer**.

* **Rollback** — `test_rollback.py`. Der wichtigste Fall ist der, in
  dem es *nichts* tun darf: ohne Backup wird `name` nicht angefasst,
  weil das die einzige Kopie sein kann, die der Nutzer noch hat.
* **CSRF** — `test_web_csrf.py`, durch `do_POST` mit echten Headern.
  Die zwei Prozeduren, die den Schutz abschalteten, tun es nicht mehr.
* **Auth-Tor der Web-UI** — `test_web_auth_gate.py`. `webauth.verify`
  war abgesichert, das Tor davor nicht; acht Prozeduren schalten es ab,
  damit ihre Seite rendert. Dazu die Regel, dass das Passwort je
  Anfrage frisch gelesen wird — sonst wirkt »Passwort setzen« erst nach
  einem Neustart.
* **Update-Fenster** — `test_update_window_enforcement.py`, beide
  Hälften samt der Frage, wessen Wochentag die Stunden nach Mitternacht
  gehören.
* **Wochenbericht-Zeitplan** — `test_weekly_schedule.py`. Beide
  Richtungen sind schlimm: immer-ja schickt den Bericht bei jedem Tick,
  immer-nein hält eine Funktion an, die erst eine Woche später auffällt.
* **`healthcheck.py`** — `test_healthcheck_exit.py`, als Unterprozess,
  weil der Exit-Code die Schnittstelle ist. Keine Prozedur hatte die
  Datei überhaupt geladen; ein Syntaxfehler darin wäre ausgeliefert
  worden.
* **`--rm`-Abhängige** — `test_netns_dependent_recreate.py`. Ein
  `AutoRemove`-Container hört im Moment des Stops auf zu existieren,
  also muss `inspect` **vor** dem Stop laufen. Die alte Prüfung war
  `"Capture config up front" in checker`, und dieser Text steht nur im
  Kommentar: den Kommentar umformulieren hätte sie rot gemacht, den
  Code verschieben nicht.
* **Ruhezeiten und Wartungsmodus** — am 06.10. erledigt.

## 1b — Der Durchlauf vom 10.10.: 39 Zusagen, die nichts hält

Audit E, isoliert in einem eigenen Worktree, auf die Zusagen angesetzt,
die beim ersten Durchlauf **nicht dran waren**. Methode: Zeile
entfernen, alle 194 Prozeduren fahren, zählen was rot wird —
`PYTHONDONTWRITEBYTECODE=1`, Absturz zählt nicht als Treffer, beidseitige
Kontrolle.

**Erledigt:** die Verdrahtung Gesundheit → Rollback. Alle **drei**
Stellen ließen sich abschalten, ohne dass eine Prozedur rot wurde —
selbst nachgestellt. Ich hatte letzte Woche `_rollback_to_old` gründlich
abgesichert und geglaubt, die Zusage sei gedeckt; gedeckt war der
Aufruf, nicht die Entscheidung ihn zu tun. Jetzt
`test_health_rollback_wiring.py`, beide Richtungen.

**Ebenfalls erledigt:** `/metrics` und `/api/status`, der größte
Einzelblock. Acht Rückbauten, alle ohne rote Prozedur — keine Prozedur
rief `_serve_metrics` oder `_serve_status_json` je auf. Jetzt werden
beide Endpunkte wirklich bedient und die Bytes zurückgelesen, gegen
einen Aufbau mit zwei Hosts, von denen einer nicht antwortet. Alle acht
Rückbauten gegengeprüft, alle rot.

**Und erledigt:** die Selbst-Update-Kette. Sechs Rückbauten, keine rot
— der Pfad, auf dem sich das Programm selbst ersetzt, und der einzige,
auf dem ein Fehler niemanden mehr hat, der ihn meldet. #43 und #62 sind
beides Narben davon. Jetzt `test_selfupdate_chain.py`: die Reihenfolge,
beide Fehlerarme getrennt, die Stop-Frist, und die Boot-Meldung, die den
Satz sucht, den das Skript wirklich schreibt.

**In der Schuldenliste** (`UNGUARDED`, `DEBT = 6`), also beweisbar
ungedeckt und nur noch verkleinerbar: Major-Bestätigung,
`MIN_IMAGE_AGE_DAYS`, Schwärzen im Audit-Trail, OOM-Erkennung, das
Altersfenster der Warteschlange, `insecure_registries`.

**Gemeldet, Anker noch nicht festgenagelt** — gehören hier hinein,
sobald sie eindeutig sind, denn ein veralteter Anker ist eine Prüfung,
die bestanden hat ohne zu laufen:

* **Container-Gruppen**, drei: die Wartezeit zwischen Containern, der
  30-Sekunden-Boden, ein Container = eine Gruppe. Die
  VPN-Sidecar-Mechanik selbst ist abgesichert.
* **Platzwarnung**, drei: Schwelle, 23-Stunden-Sperre, Klemmung 50..100.
* **Audit-Trail**, vier weitere: der Telegram-Seam (geprüft wird nur,
  wie oft `audit.record(` im Quelltext steht), der Akteur bei
  Token-Zugriff, die History-Seite, die Werte-Kürzung.
* **Restore über Discord**: 2-MB-Kappe und Schema-Prüfung ungedeckt,
  während die wortgleichen Telegram-Zeilen beide gefangen werden. Reine
  Paritätslücke.
* **`Retry-After`-Kappe** im gemeinsamen HTTP-Pfad: ein Server mit
  `Retry-After: 3600` würde ausgeschlafen. Telegram und Discord haben
  dieselbe Kappe und sind dort abgesichert.

**Und drei Dinge, die keine Durchsetzung zum Rückbauen haben, weil sie
fehlen:**

* Der **Scheduler schreibt nichts in den Audit-Trail**, obwohl
  `en.json:601` »… and the scheduler« verspricht und `audit.py` die
  Quelle `"schedule"` dokumentiert. Ein unbeaufsichtigtes Auto-Update
  hinterlässt keinen Eintrag.
* **`/api/backup_import` hat keine Größenkappe** — die 2-MB-Regel steht
  nur in den beiden Chat-Oberflächen.
* `scripts/test_dependents_recreate.py:89` kann nicht fehlschlagen:
  es sucht `"failed"`, die Meldung schreibt `"FAILED"`.

**Prozeduren, die ohne Defekt rot werden können:** vier prüfen den
Rückgabewert von `docker run` nicht und melden bei nicht beschaffbarem
Image die Zusage als gebrochen statt die Umgebung
(`test_glob.py`, `test_protect_stop.py`, `test_netns_recreate.py`,
`test_dependents_recreate.py`). `test_multihost_live.py` stirbt gegen
einen laufenden, aber leeren Daemon. Und elf Prozeduren benutzen feste
Containernamen, zwei Läufe parallel zerstören sich gegenseitig.

**Sauber gemessen:** ohne Daemon laufen alle 194 durch, in drei
Zeitzonen (+14, −11, +5:45) je ein kompletter Durchgang ohne Rot, kein
`datetime.now()` in den zeitabhängigen Prozeduren, jedes `urlopen`
gemockt.

## 2 — ~~Dieselbe 4xx-Sache in vier weiteren Kanälen~~ — gemessen, behoben

**Erledigt am 07.10.2026.** Gemessen war es nicht, was der Agent
vermutete, sondern das Gegenteil — und derselbe Fehler wie bei SMTP, nur
andersherum: keiner der Kanäle wiederholte eine 4xx, aber **alle warfen
`429 Too Many Requests` und `503 Service Unavailable` ersatzlos weg**.

ntfy.sh drosselt kostenlose Konten. Ein Schwall fehlgeschlagener Updates
ist genau der Moment, in dem eine Drosselung greift *und* in dem die
Nachricht zählt — die Meldung, die am ehesten verschwand, war die
wichtigste. Jetzt in `base.TEMPORARY_STATUS`, für alle sechs Kanäle
gleich, mit `test_channel_backoff.py` als eine Prozedur statt sechs
Kopien, und zwei Einträgen in der Ratsche.

`500` bleibt bewusst auf der »nein«-Seite: es kann ebenso ein dauerhafter
Serverfehler sein, und dann ist die Warteschlange eine Schleife.

## 3 — ~~Behauptungen im Code, die ungeprüft dastehen~~ — erledigt

**07.10.2026.** Alle sieben fahren jetzt, jede mit einem Eintrag in
`test_guard_mutations.py`: `STOP` ist falsy und sperrt seinen Kanal
(aber nicht die anderen), `usable([])` ist False, die Engine-Reihenfolge
wird mit einer Ausgabe geprüft, die **beide** Namen enthält — das ist
der Fall, für den sie existiert, und keine Fixture hatte ihn —,
`EMITTED` wird gegen dreizehn gefahrene Szenarien gehalten, und die
englischen Texte werden gelesen: `hostcheck_ready` darf nicht
»writable« sagen.

Die vier überdehnten Sätze sind korrigiert. `SSLZeroReturnError` steht
jetzt ausdrücklich als Lesart da, nicht als Messung. Die
`parseaddr`-Begründung sagt, was wirklich kaputt war: nicht der Header
auf der Leitung, sondern die ID, die an den Retry geht. Und die Zeile
»another attempt would be a second copy« sagt das nicht mehr, weil
`STOP` zwei Ursachen hat und bei einer Ablehnung nichts zugestellt
wurde.

Offen geblieben: dass `ps -aq` auch unter Podman den neuesten zuerst
listet, ist weiter **nicht gemessen** — dafür braucht es einen Podman-Host
mit mehreren Containern. Der Kommentar nennt jetzt Docker.

## 4 — Struktur und Nutzererfahrung

Sechs von sieben erledigt am 07.10.2026: `/hosts check` läuft auf einem
eigenen Thread (gemessen: `_handle_message` kehrt nach 0,00 s zurück,
die Antwort kommt trotzdem), der Prüf-Timeout ist auf die 30 s des
Betriebs angehoben, `{endpoint}` wird bei 200 Zeichen gekappt — damit
ist der letzte bekannte Gleichstands-Unterschied weg —, `MAX_QUEUED`
gilt auch beim Zurückschreiben, `/hosts CHECK` wird erkannt und
überzählige Argumente werden nicht mehr stumm verworfen, und der
Datenschutzsatz im README sagt jetzt, dass `TELEGRAM_ALLOWED_USERS`
regelt, *wer fragen darf*, nicht wer die Antwort liest.

Bei `smtp_tls` kam ein Fund dazu, den kein Audit gemeldet hatte: der
Vergleich war **case-sensitiv**. `SMTP_TLS=SSL` traf keinen der beiden
Zweige und fiel auf Klartext-SMTP durch — mit gesetztem `smtp_user`
also das Passwort unverschlüsselt auf die Leitung. Jetzt wird
kleingeschrieben verglichen, und ein unbekannter Wert wie `tls`
verbindet gar nicht erst.

**Offen:**

* **`tcp://…:2376` bekommt die unbrauchbarste Antwort.** Das ist
  Dockers eigene empfohlene Absicherung, und es gibt im ganzen Projekt
  keine TLS-Client-Zertifikate (`DOCKER_CERT_PATH`, `DOCKER_TLS_VERIFY`
  kommen in `app/` und `README.md` nirgends vor). Die Vorab-Prüfung
  meldet „Nothing answered … malformed HTTP response \x15\x03\x01".
  Zwei getrennte Dinge: die Antwort erkennen und benennen, und
  entscheiden, ob TLS-Client-Zertifikate ein eigenes Paket werden.

## 5 — Texte über 16 Sprachen

* **Der `hostdiag`-Hinweis ist hart kodiertes Englisch** und hängt in
  allen 16 Sprachen unter einem übersetzten Befund. Betrifft jetzt fünf
  Anhängepunkte. Fünf Sätze × 16 — eigenes Paket.
* `{endpoint}` wird ungekappt ausgegeben, `{error}` auf 300 Zeichen.
  Mit einem 4011-Zeichen-Endpunkt gehen die beiden Bots auseinander:
  Telegram splittet, Discord klippt. `ep[:200]` schließt es. Das ist
  der **einzige** bekannte Gleichstands-Unterschied.
* `{error}. Those are…` ergibt `…connect.. Those are…`, wenn die
  CLI-Meldung auf einen Punkt endet. In 16 Sprachen gleich.
* `zh.json`: drei der neuen Schlüssel benutzen ASCII-`, ` und `: `,
  während die Datei sonst 322× `，` und 182× `：` hat.
* „inspect" ist innerhalb einer Datei mal übersetzt, mal nicht
  (ar/hi/ja/ko/tr/zh/de/ru/uk).
* „Expected one of: {schemes}" nennt die vier empfohlenen, akzeptiert
  werden sechs. Der Satz behauptet Vollständigkeit, die er nicht hat.

## 6 — Die Suite selbst

**08.10.2026 abgearbeitet**, bis auf die zwei Posten unten.

* Die **24 Quelltext-Prüfungen, die ein Kommentar erfüllt**, sind auf 21
  gesunken, und die vier mit der größten Folge sind weg:
  * **Die Cookie-Flags** (`HttpOnly`, `SameSite=Lax`, kein `Secure`)
    waren ein grep über `web_ui.py`, erfüllt vom Kommentar direkt über
    der Zeile, die sie setzt. Jetzt wird die Anmeldung gefahren und der
    `Set-Cookie`-Header gelesen. Gegenprobe: jedes Flag entfernen macht
    rot.
  * **Der Token-Vergleich der Metrics-Schnittstelle** war
    `"compare_digest" in src`, und das Wort steht drei Zeilen höher im
    Docstring. Jetzt als Baum gelesen *und* gefahren — inklusive eines
    Tokens, das nur das Präfix teilt, was ein einschleichendes
    `startswith` fängt, das der alte grep nie gesehen hätte.
  * **`test_compose_recreate_budget.py`** hatte alle sechs Prüfungen auf
    `src.index("# Recreate service via compose.")` verankert, also auf
    einem Kommentar: ihn umzuformulieren ließ die Prozedur *abstürzen*,
    den Code zu verändern nicht. Jetzt als AST. Genau umgekehrt
    nachgemessen — die drei echten Defekte werden rot, die
    Kommentar-Umformulierung bleibt grün.
  * **`test_gluetun_docs.py`**: die beiden Nadeln über `--rm` und
    »Capture config up front« deckt jetzt
    `test_netns_dependent_recreate.py` verhaltensbasiert ab.
* **`test_trust_running.py`** benutzt das `checks`-Dict statt nackter
  `assert`s. Vorher hielt der erste Fehlschlag die anderen beiden Fälle
  auf — darunter den einzigen, der wirklich zählt: dass das
  »Laufzustand vertrauen«-Flag eine Absturzschleife *nicht* verschluckt.
  Und 14 Sekunden echte Docker-Zeit erschienen in `VERIFICATION.md` als
  zwei Prüfungen.
* **`_policy_decision`** — beide zuständigen Tests überschrieben die
  Weiterleitung, um die Regel zu prüfen. Die Weiterleitung ist jetzt ihre
  eigene Zusage: der Bot fragt den Motor, und was er zurückgibt, kommt
  unverändert heraus.

**Offen:**

* **21 Quelltext-Prüfungen**, die ein Kommentar oder Docstring erfüllen
  könnte. Die verbliebenen sichern Kleineres ab (Meldungstexte,
  Dockerfile-Zeilen, Hilfsnamen); Liste erzeugt
  `scratchpad/needles2.py`. `not in`-Prüfungen sind nicht darunter — die
  macht ein Kommentar rot, nicht grün, und das ist die sichere Richtung.
* **Flakiness, Stand 09.10.2026**: die serielle Aussage von oben ist
  **zurückgezogen** — sechs Läufe ergaben fünf grün, einen rot, immer
  auf derselben Prüfung. Ursache war das Fixture (ein Probe-Container,
  dessen Start nie geprüft wurde); behoben, danach fünf grün und ein
  sauberer SKIP. Offen bleibt: beide Live-Docker-Prozeduren benutzen
  **feste Containernamen** (`ds_trust_test`, `ds-tcp-transport-test`,
  `probe`) und einen festen Port 12375, zerstören sich also bei zwei
  gleichzeitigen Läufen gegenseitig.
* Die fünf `*_resilience.py` teilen weiter 25 identische
  Assertion-Ausdrücke. `test_channel_backoff.py` hat den wichtigsten
  Teil davon zusammengefasst; der Rest wäre Kosmetik mit Risiko.

## 7 — Entschieden, bleibt so

* `de.json` übersetzt die Metavariable zu `<endpunkt>`, die anderen 15
  behalten `<endpoint>`. Für einen deutschen Leser ist das besser, und
  der kopierbare Teil des Befehls ist unberührt.
* `fd://` und `npipe://` bleiben **akzeptiert**, werden aber nicht mehr
  empfohlen: wer so ein Setup hat, weiß es besser als unsere Liste.
* Die Vorab-Prüfung gibt es in der Web-UI nicht, und kein Text behauptet
  etwas anderes.
