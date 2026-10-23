# Docksentry — Arbeitsregeln (lokal, nie committen)

## Release-Prozess: Beta zuerst (seit 18.08.2026)

**Features und Umbauten gehen zuerst als Beta raus, nie direkt auf `:latest`.**

1. Taggen als `vX.Y.Z-beta.N` → die Pipeline baut `:beta` und das
   Versions-Tag, `:latest` bleibt unangetastet. GitHub-Release als
   **Prerelease** markieren (`gh release create --prerelease`).
   Jede Beta bekommt einen kurzen Kommentar in **Issue #63**
   (der laufende Beta-Faden): Version, Inhalt, was anzutesten ist.
   Beim Final dort zurückverlinken.
2. Reifezeit, seit 19.08. verschärft auf Wunsch beider Tester:
   **Kleines und Fixes**: 24 Stunden oder Tester-OK.
   **Majors und Kern-Umbauten** (Update-Pfad, Recreate, Multi-Host):
   ausdrückliches Grün von dem Tester, dessen Setup es betrifft —
   famewolf = Multi-Host/Gruppen, LeeNX = Podman, NotRetarded =
   Discord. Die Uhr reicht da nicht; »he's got 3 people with totally
   different setups than what he's using«.
3. Danach denselben Stand als `vX.Y.Z` taggen → `:latest` zieht nach.
   **Genauer: den Beta-Baum plus eine Zeile — `app/version.py` auf die
   Final-Nummer.** Kein neuer Code, nur der Name. Wörtlich „derselbe
   Commit" geht nicht: der trägt `2.18.0-beta.N`, und ein `v2.19.0`
   darauf veröffentlicht ein `:latest`, das sich selbst Beta nennt.
   Genau so ist `v2.18.0` entstanden — getaggt auf einem Commit mit
   `VERSION = "2.18.0-beta.35"`, zwei Monate lang unbemerkt, bis
   @famewolf am 22.10.2026 schrieb: „somehow i've ended up on a beta
   even though I never opted to leave stable". Er war es nicht; der
   Code war der freigegebene, der Name darauf war falsch. Seit
   beta.43 bricht der Build ab, wenn Tag und `version.py`
   auseinanderlaufen (`test_release_tag_guard.py`).
   Release-Notes des Finals verweisen auf die Beta-Notes.

**Einzige Ausnahme:** Fixes für akut Kaputtes (Beispiele: verklemmtes
Update-Schloss 2.11.1, sterbende Selbst-Updates 2.14.2) dürfen direkt
auf `:latest` — wer blutet, wartet nicht auf Reifezeit.

Warum: Am 18.08. gingen sieben Versionen im Stundentakt direkt auf
`:latest`. 2.12.0 brach nach zehn Minuten beim ersten Tester
(`/restore`, fehlender Import), und der 137er-Bug traf NotRetarded per
AUTO_SELFUPDATE über Nacht. AUTO_SELFUPDATE-Nutzer ziehen `:latest`
ungefragt — jede ungereifte Version landet ungefragt auf fremden
Servern.

Die lokale Instanz (docker-compose.dev.yml) läuft immer den lokalen
Build und ist damit automatisch der erste Tester — vor der Beta.

## Zähl- und Vollständigkeits-Aussagen: erst zählen, dann sagen

Sätze wie »nur X«, »der einzige«, »alle N«, »sonst nichts«, »alle
Befehle prüfen« sind **Behauptungen über eine ganze Menge** — und die
gilt es zu *zählen*, nicht zu schätzen. Ein `grep`, ein Regex-Scan, eine
Heuristik ist ein **Hinweis, kein Beweis**: er liefert Kandidaten, nie
das Ergebnis. Bevor eine solche Aussage rausgeht:

1. Die Menge vollständig **aufzählen** (alle Befehle, alle Kanäle, alle
   Aufrufer — jeden Eintrag, nicht das Muster).
2. Jeden Eintrag **einzeln** gegen die Frage prüfen — die Datei öffnen,
   nicht dem Scan glauben. Scans haben Fehlstellen: ein Befehl löste den
   Container über einen Helfer auf und lief dann doch lokal weiter, was
   das Muster nicht sah (`/logs`, `/audit`, 19.08.).
3. Das Ergebnis als **Liste** hinschreiben, nicht als Zahl — »geprüft:
   A, B, C; lokal-only war: X, Y« ist überprüfbar, »nur X« ist es nicht.

Wo möglich, die Prüfung als **Test festhalten**, damit die Aussage nicht
verrottet (siehe `test_command_host_coverage.py`: jeder
container-berührende Befehl muss host-auflösen — genau der Audit, den
sonst jemand von Hand wiederholen müsste).

**Das gilt auch für Aussagen an Andreas im Chat, nicht nur für
öffentliche Antworten.** Er baut seine Antworten an die Reporter auf dem
auf, was ich ihm hier sage — eine ungeprüfte Chat-Behauptung wird so zur
falschen öffentlichen Aussage. Der »100% sicher oder fragen«-Maßstab
beginnt beim ersten Mal, wo die Behauptung fällt, nicht erst beim
Absenden. Fällt der Beweis nicht zu führen: als offene Frage
kennzeichnen (»vermutlich der einzige, noch nicht alle 40 geprüft«),
nicht als Verdikt.

## Vor jedem Tag: die Audit, vier Blickrichtungen

**Kein Tag ohne Audit.** Vor `git tag` läuft `AUDIT.md` — vier Agenten
parallel auf `git diff v<letzter-tag>..HEAD`: Korrektheit,
Was-wäre-wenn, Gleichstand/Übersetzung, und Behauptungen gegen Code
samt Jagd auf vakuöse Prüfungen. Die Prompts stehen dort zum Kopieren,
auch in einer Fassung für andere Projekte und Werkzeuge.

Warum als Regel und nicht als gute Absicht: vor beta.35 fand der
Durchlauf elf Punkte, an denen alle 4160 Prüfungen grün waren. Zwei
davon waren einen Tag alt und hätten jemandem die Datenbank verdeckt.
Sechs Prüfungen waren vakuös — eine behauptete, `object()` sei nicht
`None`. Eine grüne Suite sagt, dass nichts bricht, was ich geprüft
habe; sie sagt nichts über das, was ich zu prüfen geglaubt habe.

Drei Dinge, die die Regel ausmachen und ohne die sie nichts wert ist:

* **Jeden Befund selbst nachstellen**, bevor er zur Änderung wird. Ein
  Agentenbericht ist ein Hinweis, kein Beweis — dieselbe Regel wie für
  `grep`.
* **Der Baum steht still**, solange die Audit läuft. Sonst messen die
  Agenten gegen etwas, das es nicht mehr gibt.
* Danach **`VERIFICATION.md` neu schreiben** und die Suite laufen
  lassen, erst dann taggen.

## Ein Test darf nicht ersetzen, was er prüft

Am 06.10.2026 hat eine Mutationsprobe über die ganze Suite zehn
Kernzusagen gefunden, deren Durchsetzung man löschen kann, ohne dass
**eine** von 181 Prozeduren rot wird — Rollback, Ruhezeiten, Pin,
`EXCLUDE_CONTAINERS`, CSRF. 4272 Prüfungen, und diese zehn Dinge hielt
nichts. Drei Ursachen, immer dieselben:

1. **Der Test ersetzt die Funktion, die er prüfen soll.**
   `test_weekly_report_channels.py` setzt `should_send_now = lambda: True`
   und prüft danach den Zeitplan nicht mehr; zwei von drei
   CSRF-Prozeduren setzen `_check_csrf = lambda: True`. Ein Mock an der
   Stelle, um die es geht, ist keine Prüfung, sondern ihre Abwesenheit.
2. **Der Test prüft eine Kopie der Produktionslogik.** `test_labels.py`
   hat eine Überschrift »exclude decision (mirrors
   get_running_containers)« und schreibt den Ausdruck danach ab. Die
   Kopie war mit sich einig, während das Original alles tun konnte.
3. **Die Prüfung ist ein grep, der auf einem Kommentar sitzt.**
   `"Capture config up front" in checker` — dieser Text steht
   ausschließlich in einem Kommentar. Und ein **auskommentierter**
   Aufruf erfüllt jeden grep, der ihn sucht: so war `/hosts check` auf
   beiden Verbindungen tot und drei Prüfungen grün.

Daraus drei Regeln:

* **Die Zusage fahren, nicht die Umgebung.** Was geprüft wird, ist das,
  was hinten herauskommt — die Liste, die Nachricht, die Antwort —, nicht
  dass eine Zeile existiert. Ein Stand-in ersetzt die *Umgebung* (Socket,
  Netz, Uhr), niemals die Entscheidung.
* **Immer die Gegenrichtung mitprüfen.** »Wird zurückgehalten« ist
  wertlos ohne »und ohne den Grund geht es raus«: sonst besteht die
  Prüfung auch auf einem Stand-in, der nie etwas sendet. Genau so war
  mein eigener Ruhezeiten-Test erst grün.
* **Keine Prüfung, die von der Wanduhr abhängt.** Dieselbe Datei hing an
  der echten Uhrzeit, und weil es 01:00 war, bestanden die
  Ruhezeit-Prüfungen aus dem falschen Grund. Zeit wird übergeben.

Festgehalten als Prüfung, nicht als Vorsatz: `test_guard_mutations.py`
bricht je Zusage die Zeile, die sie durchsetzt, und verlangt, dass eine
genannte Prozedur rot wird. Was dort noch in `UNGUARDED` steht, ist
offene Schuld und darf nur kürzer werden.

## Test-Hosts der lokalen Instanz

Die lokale Instanz hängt an mehreren Endpunkten; die Adressen stehen in
`docker-compose.dev.yml` und bleiben dort (gitignored).

* **podman** — Podman über den lokalen Socket. LeeNX' Fall.
* **srv30** — Docker über TCP.
* **srv40** — Docker über SSH.
* **srv20** — **die Tot-Prüfung.** Der Host ist absichtlich nicht
  erreichbar. Er hat den Timeout-Pfad belegt, den kein Mock beweist:
  `/status` lieferte gar nichts statt einer Zeile pro Host, bis ein
  echter toter Host es zeigte (#2). Festgehalten in
  `test_multihost_routing.py` und im Kommentar in `telegram_bot.py`.

**srv20 gehört nicht in den Dauerbetrieb.** Jeder geplante Lauf kostet
sonst 30 Sekunden Timeout und schreibt eine Fehlerzeile ins Log, die
aussieht wie ein echter Ausfall — am 21.09. stand sie mitten in einem
Update-Durchgang und war das Erste, was ins Auge fiel. Zum Testen die
auskommentierte Zeile über `DOCKER_HOSTS` in `docker-compose.dev.yml`
wieder einsetzen, danach wieder herausnehmen.

## Verbindungsabbrüche erzwingen, statt auf sie zu warten

Fragen an den Discord-Gateway — hält ein Resume, greift ein Backoff,
was steht im Log — hingen bisher an einem echten Abbruch, und der kommt,
wann er will. Am 22.09. hieß das zweimal »wir können nur warten«, und
genau in dieser Wartezeit sind zwei falsche Theorien entstanden.

Der Socket lässt sich von außen fallen lassen:

```bash
PID=$(docker inspect docksentry --format '{{.State.Pid}}')
sudo nsenter -t $PID -n ss -K dst 162.159.0.0/16 dport = 443
```

Der Client verbindet eine Sekunde später neu und versucht dabei genau
den Weg, um den es geht. Aus Stunden werden zehn Sekunden, beliebig oft
wiederholbar — so ließ sich die Resume-Frage mit einer **Kontrolle**
beantworten statt mit einer Vermutung: nackte URL → `invalidated`
(19:08), mit Query → `session resumed` um 19:09:18, 19:09:31 und, auf
dem eingecheckten Stand, 19:16:14.

Zwei Dinge dazu. Die Gateway-IP wechselt, deshalb das ganze Netz statt
einer Adresse — ein Kill auf die alte IP geht wortlos ins Leere und
sieht aus wie »nichts passiert«. Und ein gekillter Socket schickt **kein
Close-Frame**: was am Close-Code hängt, lässt sich so nicht prüfen.
