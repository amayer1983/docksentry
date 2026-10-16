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

**Und erledigt:** Container-Gruppen und Platzwarnung. Die Gruppen-Regeln
sind das, was eine Gruppe zur Gruppe macht — eine Datenbank und ihre
Anwendung in derselben Sekunde neu zu bauen heißt, dass die Anwendung
gegen eine Datenbank startet, die noch nicht hört. Die Platzwarnung
scheitert in beide Richtungen schlimm: feuert sie nicht, läuft die
Platte still voll; feuert sie bei jedem Tick, schaltet der Nutzer den
Kanal stumm und verliert den nächsten echten Alarm mit.

Zwei Dinge dabei, die ich offen hinschreibe. Die beiden Wartezeiten im
Stapel werden als **Baum** gelesen, nicht gefahren: `_process_update_batch`
braucht ein Dutzend Mitspieler, bevor es dort ankommt, und ich habe beim
sechsten Stub abgebrochen — ein so weit gestützter Prüfstand misst sein
eigenes Gerüst. Und die erste Fassung war schlechter als beides, sie hat
die Schleifenbedingung des Motors abgeschrieben und die Kopie geprüft.

**In der Schuldenliste** (`UNGUARDED`, `DEBT = 6`), also beweisbar
ungedeckt und nur noch verkleinerbar: Major-Bestätigung,
`MIN_IMAGE_AGE_DAYS`, Schwärzen im Audit-Trail, OOM-Erkennung, das
Altersfenster der Warteschlange, `insecure_registries`.

**Gemeldet, Anker noch nicht festgenagelt** — gehören hier hinein,
sobald sie eindeutig sind, denn ein veralteter Anker ist eine Prüfung,
die bestanden hat ohne zu laufen:


**Erledigt am 11.10.2026:** der Audit-Trail (die vier offenen Punkte),
die Restore-Parität und die `Retry-After`-Kappe.

Der **Telegram-Seam** wird jetzt gefahren statt gezählt. Was dort stand,
war `tg.count('audit.record("telegram"') == 1` — eine Zahl im Quelltext,
die ein auskommentierter Aufruf genauso erfüllt, und genau so war
`/hosts check` auf beiden Verbindungen tot bei drei grünen Prüfungen.
Jetzt geht der Befehl durch `_handle_message` und der Eintrag wird
zurückgelesen, mit der Gegenrichtung: ein Befehl von einem Fremden
hinterlässt keine Spur (der Seam sitzt *nach* der Auth-Prüfung), eine
normale Chat-Nachricht auch nicht, und ein aufgezeichneter Befehl ist
weiter ein beantworteter. Dafür `/help`, nicht `/stop`: der
`/stop`-Prüfstand brauchte Motor, Hosts und Lifecycle-Modul, bevor er
irgendetwas Beobachtbares erreichte — so weit gestützt misst er sein
eigenes Gerüst.

Dazu der **Akteur bei Token-Zugriff** (ein Prometheus-Scrape ist
`token:prom` über die Quelle `api`, kein Mensch), die
**History-Seite** (gerendert und die Bytes zurückgelesen: Zeit, Akteur,
Befehl, Ziel, `id="audit"` zum Finden, geschärft, und der
Schwärzungs-Marker statt eines Werts) und die **Werte-Kürzung** — und
zwar am Eintrag auf der Platte, nicht nur am Rückgabewert von `redact`.
Vier Einträge in der Ratsche, alle vier gegengeprüft.

`test_restore_parity.py` fragt beide Türen dasselbe — die Kappe, das
`schema_version`-Siegel, Nicht-JSON, eine Datei ohne `.json` — und
dazu die Gegenrichtung, dass eine echte Sicherung weiter durchkommt.
Die erste Fassung war grün und wertlos: sie las die Antwort nach den
Wörtern »backup« und »json«, und `restore_offer` beginnt mit »A backup
arrived«, während die Antwort den Dateinamen `backup.json` zitiert.
Beide Ablehnungsprüfungen waren vom Annahmepfad erfüllt — die
Gegenprobe hat es gesagt, nicht das Lesen. Jetzt wird gemessen, ob
*angeboten* wurde: bei Discord der Bestätigungs-Token, bei Telegram die
eigene `_pending_restores`-Tabelle. Die Zeile, die ich zuerst
überwacht glaubte, ruft Telegram auf diesem Pfad nie auf.

Die `Retry-After`-Kappe fährt jetzt zwei Fälle durch
`post_json_with_retry`: zwei Sekunden werden abgewartet und der Versuch
wiederholt, eine Stunde nicht — die Nachricht geht in die
Warteschlange, statt den Scheduler-Thread eine Stunde schlafen zu
legen.

**Die drei Dinge, denen die Durchsetzung ganz fehlte — erledigt:**

* Der **Scheduler schreibt jetzt in den Audit-Trail**. Die
  Connections-Seite verspricht »… and the scheduler« seit jeher, und
  `record` dokumentiert die Quelle `"schedule"` mit dem Akteur
  `"system"` — aber jede Aufrufstelle im Programm war eine Oberfläche.
  Ausgerechnet das unbeaufsichtigte Update, die einzige Art, bei der
  niemand zugesehen hat, hinterließ keine Spur. Mit Namen, nicht mit
  Anzahl: »2 Container« beantwortet keine Frage, die man ein Woche
  später an ein Protokoll stellt.
* **`/api/backup_import` hat dieselbe Obergrenze wie die beiden Chats.**
  Vorher wurde `Content-Length` geglaubt und am Stück gelesen — ein
  angekündigtes Gigabyte war ein Gigabyte im Speicher, bevor irgendetwas
  hinsah. Und es wird höchstens die Kappe gelesen, denn die angegebene
  Länge ist das Wort des Aufrufers.
* `test_dependents_recreate.py` sucht nicht mehr nur `"failed"`, wo die
  Meldung `"FAILED"` schreibt.

**Erledigt am 11.10.2026: Prozeduren, die ohne Defekt rot werden
konnten.** Vollständig aufgezählt statt geschätzt — es waren neun
Dateien, 23 feste Bezeichner auf dem Daemon (15 Containernamen, ein Pod,
zwei gebaute Image-Tags, zwei Podman-Verbindungen, zwei feste Ports) und
18 Startaufrufe, von denen 15 bei nicht beschaffbarem Image die Zusage
als gebrochen gemeldet hätten; zwölf lasen den Rückgabewert gar nicht.
Null davon waren pro Lauf eindeutig.

Gemessen, nicht behauptet: mit einem fremden `ds_ctf-a-even` auf dem
Daemon hat die alte `test_glob.py` ihn entfernt — danach ist er noch da.
Das ist das ganze Argument für `livefixture.TAG`.

Jetzt ein gemeinsames `scripts/livefixture.py`: `name()` hängt die PID
an, `free_port()` ersetzt 12375 und 8791, `start()` liest den
Rückgabecode und macht aus einer Absage einen SKIP, der die bereits
erreichten Prüfungen **mitnimmt** statt sie wegzuwerfen. Dazu
`test_multihost_live.py` bringt seinen eigenen Container mit (vorher
`sorted(remote_names)[0]` → IndexError gegen einen leeren Daemon, also
ein Absturz, der weder Prüfung noch SKIP ist), `test_image_inherit.py`
sagt einen fehlgeschlagenen Build laut (vorher `if ok:` ohne `else`:
fünf Prüfungen verschwanden und PASS stand trotzdem da),
`test_podman_live.py` prüft, ob Podman überhaupt antwortet, und
`test_remote_transports.py` wirft beim dind-SKIP nicht mehr die zwei
Dockerfile-Prüfungen weg.

Als Wächter festgehalten: `test_live_fixtures.py` liest die ganze Suite
als Baum und wird rot, sobald eine Prozedur wieder einen festen Namen
anlegt oder einen Container startet, ohne das Ergebnis anzusehen —
beides mit Gegenprobe, dass der Scanner wirklich beides findet.

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

* **`tcp://…:2376`: die Meldung ist erledigt (11.10.2026), die
  Client-Zertifikate nicht.** Die Vorab-Prüfung sagt jetzt
  `needs_tls` statt „Nothing answered" — in 16 Sprachen, und sie sagt
  ausdrücklich, dass die Lücke bei mir liegt und nicht beim Leser. Das
  ist auch nötig: ich habe am 18.08. in #2 selbst geschrieben,
  Docksentry „then just talks to `tcp://host:2376`", und das stimmt
  nicht. Wer dem folgte, bekam seine Maschine als unerreichbar gemeldet.

  Entschieden wird nicht am Port, sondern gemessen — `speaks_tls()`
  bietet einen Handshake an. Nachgestellt gegen zwei echte dinds: mit
  `DOCKER_TLS_CERTDIR=/certs` kommt der Handshake zustande, dasselbe
  Image ohne TLS lehnt mit `WRONG_VERSION_NUMBER` ab. Beides als
  Prüfung festgehalten, die Gegenrichtung mit: ein totes Port und eine
  echte Ablehnung dürfen nicht als TLS gelesen werden. Nebenbei
  gelernt: Docker 29.5 sagt es inzwischen selbst („Client sent an HTTP
  request to an HTTPS server"), Podman sagt nur „ping response was
  400" — deshalb wird gefragt und nicht geraten.

  **Offen bleibt:** TLS-Client-Zertifikate selbst
  (`DOCKER_CERT_PATH`, `--tlsverify --tlscacert --tlscert --tlskey`).
  Bewusst hinter den Kern-Umbau gestellt: es sind nur zwei
  `-H`-Aufrufstellen, aber die Zertifikate gehören pro Verbindung
  konfiguriert, und diese Schicht entsteht erst dort — vorher gebaut
  heißt zweimal gebaut. Gefragt hat danach übrigens niemand; die
  einzige Erwähnung von 2376 im Tracker ist meine eigene.

## 5 — Texte über 16 Sprachen

**Erledigt am 11.10.2026:** der `hostdiag`-Hinweis ist übersetzt. Fünf
Sätze × 16 Sprachen, und alle fünf Anhängepunkte fragen jetzt in der
Sprache des Lesers — der geplante Durchgang, `/check`, `/status`, die
Vorab-Prüfung in Telegram und die in Discord. Ein deutscher Leser bekam
die Diagnose auf Deutsch und die eine Zeile, die ihm sagt was zu tun
ist, auf Englisch.

Der englische Boden bleibt in `hostdiag._EN` und wird gegen `en.json`
gehalten: ein Aufrufer ohne Übersetzer darf den Satz nicht verlieren,
und zwei Fassungen desselben Satzes dürfen nicht auseinanderlaufen.
Was dort an Prüfungen stand, war `"hostdiag.hint" in sched` und zwei
`count() >= N` — jetzt als Baum gelesen: jede `hostdiag.hint`-Aufrufstelle
in `app/` muss ein `t=` mitgeben, und es sind genau fünf.

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

**Erledigt am 16.10.2026 — die Prüfungen, die nur ihre eigene Form
lasen.** Audit 4 hat sie als Mutation belegt, jede einzelne, und hier
stehen nur die, die ich selbst nachgefahren habe: die Gruppen-Wartezeit
und die 30-Sekunden-Untergrenze (vier Mutationen, vier Mal grün),
`shlex.quote` beim Image, die Boot-Meldung nach einem zurückgerollten
Recreate, `/selfupdate previous`, der `return` nach einem
fehlgeschlagenen Pull, der Kurzschluss bei identischem Image, und das
Fenster von 700 Zeichen in `test_ssh_hint.py`, das nur die erste von
drei Aufrufstellen sah.

Zwei Dinge dabei, die ich hinschreibe, weil sie dieselbe Lehre zweimal
sind. Die Gegenprobe hat *mich* zweimal erwischt: der Pull-Fehler blieb
grün, weil der Prüfstand dem Code einen zweiten Grund zum Anhalten gab
(gleiches Image), und die drei neuen Selbst-Update-Prüfungen waren
zuerst grün, weil `run()` schon an der eigenen Container-Suche
ausstieg. Beide Male hat die Gegenrichtung es gesagt, nicht das Lesen.

Und die 23 Stunden der Platzwarnung sind jetzt bei 23 geprüft, nicht
bei 24 — die Prüfung hieß „after twenty-three" und testete 24, also
hielt die dokumentierte Zahl nichts.

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

## 8 — Ein stiller Kanal ist nur im Log sichtbar

**Aufgekommen am 16.10.2026 in #63.** @NotRetarded konnte nicht selbst
feststellen, warum seine Mails ausbleiben — er musste fragen. Der
Test-Knopf beweist einen Kanal im Moment des Tests; ein Kanal, der
danach verstummt, taucht nirgends in der Oberfläche auf. Der Grund
steht ausschließlich im Container-Log (`SMTP error: …`).

Gemessen, damit die Diagnose nicht daran hängt: eine tote SMTP bringt
die anderen Kanäle *nicht* zum Schweigen (abgelehnt: Discord und
Telegram in 0,0 s; angenommen-und-stumm: beide nach 15 s), und der
Wochenbericht geht per Mail raus. Die Verdrahtung ist also in Ordnung.

Was fehlt, ist die Anzeige: letzter Versand je Kanal, Zeitpunkt und
Grund, auf der Connections-Seite. Die Daten dafür entstehen bereits —
`_dispatch` fängt die Ausnahme pro Kanal und druckt sie, und der
Monitor liest seit #66 den Rückgabewert. Sie werden nur nirgends
aufgehoben.

## 7 — Entschieden, bleibt so

* `de.json` übersetzt die Metavariable zu `<endpunkt>`, die anderen 15
  behalten `<endpoint>`. Für einen deutschen Leser ist das besser, und
  der kopierbare Teil des Befehls ist unberührt.
* `fd://` und `npipe://` bleiben **akzeptiert**, werden aber nicht mehr
  empfohlen: wer so ein Setup hat, weiß es besser als unsere Liste.
* Die Vorab-Prüfung gibt es in der Web-UI nicht, und kein Text behauptet
  etwas anderes.
