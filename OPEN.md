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

## 1 — Zusagen, die nichts absichert

Aus der suiteweiten Mutationsprobe. Jede Zeile heißt: die Durchsetzung
lässt sich löschen, und keine der 184 Prozeduren wird rot. Die ersten
zwei stehen als Schuld in `scripts/test_guard_mutations.py` und können
nur kürzer werden; die anderen vier noch nicht einmal dort.

| Zusage | Stand | Wo |
|---|---|---|
| Rollback nach fehlgeschlagenem Update | **nachgestellt** (0 rot) | `update_checker._rollback_to_old` |
| CSRF-Schutz der Web-UI | **nachgestellt** (0 rot) | `web_ui._check_csrf` |
| Auth-Tor der Web-UI | gemeldet | `web_ui._check_auth` |
| Wochenbericht-Zeitplan | gemeldet (der eigene Test ersetzt die Funktion) | `weekly_report.should_send_now` |
| Update-Fenster | gemeldet (3 Proben) | `update_window.is_window_open` |
| `healthcheck.py` wird von keiner Prozedur geladen | gemeldet | `app/healthcheck.py`, `Dockerfile:90` |
| `--rm`-Abhängige neu bauen | gemeldet; die Prüfung sitzt auf einem Kommentar | `update_checker` ~:2999 |

Rollback zuerst. Es ist die Zusage aus README:37 und :65, und sie ist
der Grund, warum jemand ein Werkzeug unbeaufsichtigt arbeiten lässt.

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

* **12 nur-durch-Kommentar und 22 nur-durch-Docstring erfüllbare
  Quelltext-Prüfungen** (von 554 insgesamt). Liste beim Audit; die
  schädlichen sind die mit „code: agrees"-Etikett.
* `test_compose_recreate_budget.py` — alle sechs Prüfungen sind Greps,
  verankert auf einem Kommentar. Formuliert jemand den Kommentar um,
  **stürzt die Prozedur ab**, statt zu urteilen.
* `test_trust_running.py` benutzt nacktes `assert` statt des
  `checks`-Dicts: 14,2 s echte Docker-Laufzeit erscheinen in
  `VERIFICATION.md` als zwei Prüfungen.
* `telegram_bot._policy_decision` — die Delegation ist ungeprüft, weil
  beide zuständigen Tests sie überschreiben. Die Logik am Motor ist
  abgesichert, die Verdrahtung nicht.
* Die fünf `*_resilience.py` teilen 25 identische Assertion-Ausdrücke
  und 8 identische Prüfnamen. Zusammenlegen zu einer tabellengetriebenen
  Prozedur: ja. Löschen: nein — siehe Abschnitt 2, der vierfach kopierte
  Grep ist dort der einzige Wächter.
* **Parallellauf (`-P 8`) ist flaky**: `test_multihost_live.py` (3/3
  rot), `test_remote_transports.py` (1/3). Seriell nicht nachstellbar —
  ich habe alle drei Verdächtigen 5× seriell laufen lassen, 5/5 grün.
* Laufzeit: 126,9 s seriell, sechs Prozeduren machen 69 % davon aus.
  `test_trust_running` wartet auf einen echten Healthcheck-Umschlag —
  die 14 s sind die Sache, nicht Verschwendung.

## 7 — Entschieden, bleibt so

* `de.json` übersetzt die Metavariable zu `<endpunkt>`, die anderen 15
  behalten `<endpoint>`. Für einen deutschen Leser ist das besser, und
  der kopierbare Teil des Befehls ist unberührt.
* `fd://` und `npipe://` bleiben **akzeptiert**, werden aber nicht mehr
  empfohlen: wer so ein Setup hat, weiß es besser als unsere Liste.
* Die Vorab-Prüfung gibt es in der Web-UI nicht, und kein Text behauptet
  etwas anderes.
