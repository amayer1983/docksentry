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

## 3 — Behauptungen im Code, die ungeprüft dastehen

Alle aus Audit D, alle durch Rückbau in einer Kopie belegt: der Rückbau
ließ die Suite grün.

* `notify_retry.STOP.__bool__` → `True` zurückgeben: nichts wird rot.
  Folge wäre, dass `flush` „delivered a held smtp message" für eine
  nicht zugestellte Mail loggt.
* `usable([])` → die `bool(findings)`-Wache entfernen: nichts wird rot.
* `blocked.add` im `STOP`-Zweig entfernen: nichts wird rot.
* Die Engine-Reihenfolge („podman zuerst") umdrehen: nichts wird rot.
  Die podman-Fixture enthält das Wort „docker" gar nicht, modelliert
  also nicht den Fall, für den die Reihenfolge laut Kommentar existiert.
* `KINDS` ↔ was `preflight` wirklich ausgibt: `EMITTED` existiert jetzt,
  aber nichts hält die beiden Listen gegeneinander.
* Die Wording-Prüfung greift nur, wenn eine neue Befundart *zusätzlich*
  in `KINDS` landet — eine in `preflight` neu erfundene Art ohne
  Übersetzung bleibt unbemerkt.
* Kein Test liest die Sprachtexte: `hostcheck_ready` auf „is fully
  writable" ändern → grün.

Und zwei Sätze, die mehr behaupten als gemessen wurde:

* `smtp.py` ~:193 — die `parseaddr`-Begründung sagt, ein
  Submission-Server schreibe die kaputte ID um. Gemessen war der Header
  auf der Leitung in beiden Varianten gültig; kaputt war nur die ID im
  Log und im Retry-Aufruf. Der Fix stimmt, die Begründung ist zu groß.
* `smtp.py` ~:92 — `SSLZeroReturnError` als Handshake-Abbruch
  nachgemessen: ließ sich auf diesem Pfad **nicht** erzeugen
  (`close_notify` kam als `b''` ohne Ausnahme). `SSLEOFError` stimmt.
* `notify_retry.py` ~:230 — „another attempt would be a second copy
  rather than a second chance". `STOP` hat zwei Ursachen; bei einer
  Ablehnung ist nichts zugestellt und „second copy" ist falsch.
* `hostcheck.py` ~:201 — „`ps -aq` lists the NEWEST first" auf Docker
  29.5.3 bestätigt, für **Podman nicht gemessen**, und der Kommentar
  nennt keine Engine.
* `hostcheck.py` ~:26 und `README.md` ~:507 — „fails the first update
  with a 403" ist **nicht gemessen** und nicht als Annahme gekennzeichnet.

## 4 — Struktur und Nutzererfahrung

* **`/hosts check` blockiert den Telegram-Listener.** `_handle_message`
  läuft inline in der `getUpdates`-Schleife, `hostcheck._run` hat
  `timeout=15` bei bis zu vier Aufrufen. Gemessen 15,0 s bei einem
  gefilterten Port; rechnerisch bis 60 s. Discord macht es richtig
  (defer + Worker-Pool). Jedes kommandierberechtigte Gruppenmitglied
  kann das wiederholen.
* **`tcp://…:2376` bekommt die unbrauchbarste Antwort.** Das ist
  Dockers eigene empfohlene Absicherung, und es gibt im ganzen Projekt
  keine TLS-Client-Zertifikate (`DOCKER_CERT_PATH`, `DOCKER_TLS_VERIFY`
  kommen in `app/` und `README.md` nirgends vor). Die Vorab-Prüfung
  meldet „Nothing answered … malformed HTTP response \x15\x03\x01".
  Zwei Dinge: die Antwort erkennen und benennen — und entscheiden, ob
  TLS-Client-Zertifikate ein eigenes Paket werden.
* **Der Datenschutzsatz im README ist zu kurz gegriffen.** Habe ich
  selbst geschrieben: `TELEGRAM_ALLOWED_USERS` begrenzt, *wer fragen
  darf*, nicht *wer die Antwort sieht* — die geht immer an `chat_id`,
  also liest die ganze Gruppe Endpunkt, Container-Namen und bis zu 300
  Zeichen fremdes stderr. Discord ist standardmäßig ephemeral.
* **Der Vorab-Prüfungs-Timeout ist 15 s, produktiv sind es 30.** Ein
  Host, der 15–30 s braucht, gilt in der Prüfung als unerreichbar und
  läuft im Betrieb.
* **`/hosts CHECK` fällt stumm auf die Hostliste durch** —
  `_parts[1] == "check"` ist case-sensitiv, und `_parts[3:]` wird
  kommentarlos verworfen.
* **`smtp_tls` wird nicht validiert.** Ein Wert außerhalb
  `ssl`/`starttls`/`none` — etwa `tls` — schickt stillschweigend
  Klartext-SMTP mit dem Passwort. Vorbestehend.
* **`MAX_QUEUED` gilt auf dem Rückschreibe-Pfad nicht.**
  `self._items = keep + self._items` kappt nicht. Gemessen: 620
  Einträge bei 20 neuen pro Durchgang über 15 Minuten; begrenzt nur
  durch das Altersfenster.

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
