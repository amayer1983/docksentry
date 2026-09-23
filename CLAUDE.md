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
beantworten statt mit einer Vermutung: nackte URL → `invalidated`, mit
Query → dreimal `session resumed`.

Zwei Dinge dazu. Die Gateway-IP wechselt, deshalb das ganze Netz statt
einer Adresse — ein Kill auf die alte IP geht wortlos ins Leere und
sieht aus wie »nichts passiert«. Und ein gekillter Socket schickt **kein
Close-Frame**: was am Close-Code hängt, lässt sich so nicht prüfen.
