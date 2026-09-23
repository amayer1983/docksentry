# Audit vor der Veröffentlichung

Vier Blickrichtungen, parallel, auf den Diff seit dem letzten Tag. Vor
jedem Tag, ohne Ausnahme — die Regel steht in CLAUDE.md, das Handwerk
hier.

## Was es bringt

Vor `v2.18.0-beta.35` (23.09.2026) fand der Durchlauf **elf** Punkte, die
alle Tests grün gelassen hatten:

* zwei gefährliche, beide einen Tag alt: `/audit` bot an, ein fremdes
  Volume über unser eigenes Datenverzeichnis zu hängen, und behauptete
  „nichts hier hält diese Pfade", wo die Wahrheit „ich konnte es nicht
  feststellen" war;
* einen, den nur echtes Nachmessen findet: `docker inspect` beendet sich
  mit 1, wenn *eine* ID unbekannt ist, und druckt trotzdem gültiges JSON
  für alle anderen — wir warfen die ganze Antwort weg;
* **sechs vakuöse Prüfungen**, darunter eine, die behauptete, `object()`
  sei nicht `None`, und eine, die grün lief, wenn man die abgesicherte
  Änderung rückgängig machte;
* drei Kommentare, die als Tatsache hinstellten, was eine spätere eigene
  Messung widerlegt hatte.

Die vierte Richtung — Behauptungen gegen Code, und Jagd auf vakuöse
Tests — war die ergiebigste. Wenn nur eine läuft, dann die.

## Wie

Alle vier in **einer** Nachricht starten, damit sie nebenher laufen.
`{RANGE}` ist der Vergleich, meist `v<letzter-tag>..HEAD`.

Vorher `git status` prüfen: läuft der Baum während des Audits weiter,
messen die Agenten gegen etwas, das es nicht mehr gibt. Passiert das
doch, sagen sie es — aber besser, es passiert nicht.

### 1 — Korrektheit

> Repository: `{PFAD}`. Prüfe `git diff {RANGE}` **nur auf
> Korrektheitsfehler**. Lies die umliegenden Dateien; der Diff allein
> reicht nicht.
>
> Schwerpunkte: `{DATEI}` — `{FUNKTION}`, `{FUNKTION}`. Achte auf:
> Zustand, der einen Versuch überlebt, obwohl er es nicht sollte; Zähler,
> die nie zurückgesetzt werden; Pfade, auf denen eine Schleife ohne
> Zurückhalten läuft; None-Behandlung; ein Name, der auch leer sein kann.
>
> Melde **nur** Fehler, zu denen du ein konkretes Szenario benennen
> kannst: diese Eingabe oder dieser Zustand → dieses falsche Verhalten.
> Kein Stil, keine Namen, keine fehlenden Tests. Findest du in einem
> Bereich nichts, sag das ausdrücklich.
>
> Ausgabe: klassifizierte Liste. Je Fund: Schwere (blocker / should-fix /
> minor), `datei:zeile`, ein Satz zum Defekt, ein Satz zum Szenario.

### 2 — Was-wäre-wenn

> Repository: `{PFAD}`. `{EIN SATZ, WAS DAS PROGRAMM IST UND WO ES
> LÄUFT}`.
>
> Nimm `git diff {RANGE}` und stelle die Was-wäre-wenn-Fragen — nicht
> „ist der Code richtig", sondern „was passiert einem echten Nutzer in
> einer ungewöhnlichen Lage".
>
> Arbeite diese Lagen konkret durch: `{LISTE — fehlende Rechte, sehr
> großer Bestand, Netz weg, fremde Laufzeitumgebung, Upgrade von der
> Vorversion, ein Dienst, den der Nutzer gar nicht eingerichtet hat}`.
>
> Melde nur konkrete Risiken mit einem plausiblen Nutzer und einer
> plausiblen Folge. Ordne sie: blocker / should-fix / im Release-Text
> erwähnenswert. Ist eine Lage abgedeckt, sag es in einer Zeile und geh
> weiter.

### 3 — Gleichstand und Übersetzung

> Repository: `{PFAD}`. Es hat `{N}` Oberflächen — `{LISTE}` — und
> `{N}` Sprachen in `{PFAD ZU DEN SPRACHDATEIEN}`.
>
> Prüfe `git diff {RANGE}` auf **Gleichstand und Lücken**.
>
> 1. Sagen alle Oberflächen dasselbe über denselben Zustand? Lässt eine
>    etwas weg, was die andere zeigt? Nenne jeden Zustand, in dem sie
>    sich widersprechen.
> 2. Für **jeden** neuen Text: existiert er in **allen** Sprachdateien?
>    Ist jeder `{platzhalter}`, den der Aufrufer setzt, in jeder Sprache
>    vorhanden — und keiner zu viel? Zähl auf, stichprobe nicht.
> 3. Ist irgendwo Englisch in einer nicht-englischen Datei stehen
>    geblieben?
>
> Klassifizierte Liste. Ist ein Bereich sauber, eine Zeile.

### 4 — Behauptungen gegen Code *(die wichtigste)*

> Repository: `{PFAD}`. Regel des Betreuers: eine Behauptung, die sich
> nicht durch etwas tatsächlich Ausgeführtes belegen lässt, darf nicht
> als Tatsache dastehen — nicht im Kommentar, nicht in der Doku, nicht
> im Release-Text.
>
> 1. Lies **jeden** im Diff hinzugefügten Kommentar und Docstring. Prüfe
>    jede Tatsachenbehauptung gegen den Code, wie er **jetzt** dasteht.
>    Melde alles, was falsch, übertrieben oder eine ältere Fassung der
>    Änderung beschreibt.
> 2. Beschreiben die neuen nutzersichtbaren Texte, was der Code wirklich
>    tut? Ist der Rat darin richtig und befolgbar?
> 3. Prüfe die im Diff geänderten Projektdokumente auf Behauptungen, die
>    nie nachgemessen wurden. Stimmen gezeigte Befehle mit dem Repo
>    überein?
> 4. **Jagd auf vakuöse Prüfungen**: welche Prüfung liefe auch grün, wenn
>    man den abgesicherten Code löschte oder zurücknähme? Je Fund die
>    Begründung. Das ist der wertvollste Teil — sei feindselig.
>
> Klassifizierte Liste, `datei:zeile`, die Behauptung, und was der Code
> tatsächlich tut.

## Was danach passiert

**Jeden Befund selbst nachstellen, bevor er zur Änderung wird.** Die
Agenten lagen hier richtig — aber das war *nachgemessen*, nicht
geglaubt: jeder Blocker wurde erst mit einem kleinen Prüfstück
reproduziert, dann gefixt. Ein Agentenbericht ist ein Hinweis, kein
Beweis; dieselbe Regel wie für `grep`.

Danach: Suite laufen lassen, `VERIFICATION.md` neu schreiben (sie zählt
die Prüfungen und wird sonst schal), erst dann taggen.

## Für andere Projekte und Werkzeuge

Dasselbe Gerüst ohne Docksentry darin. Die Reihenfolge ist Absicht: die
ersten beiden finden Fehler im Code, die letzte findet Fehler in dem,
was man über den Code *glaubt* — und die überleben Releases am
längsten.

> Prüfe `{DIFF ODER DATEIEN}` in `{PFAD}`. Vier getrennte Durchgänge,
> jeder als eigene Aufgabe:
>
> 1. **Korrektheit** — nur Defekte mit konkretem Fehlszenario: diese
>    Eingabe → dieses falsche Verhalten. Kein Stil.
> 2. **Was-wäre-wenn** — was passiert einem echten Nutzer in einer
>    ungewöhnlichen Lage: keine Rechte, sehr viele Daten, Netz weg,
>    fremde Umgebung, Upgrade von der Vorversion.
> 3. **Gleichstand** — sagen alle Oberflächen, Kanäle, Sprachen dasselbe
>    über denselben Zustand? Vollständig aufzählen, nicht stichproben.
> 4. **Behauptungen gegen Code** — jeder neue Kommentar, jeder Text, jede
>    Doku-Zeile gegen den Code von jetzt. Und: welche Prüfung liefe auch
>    grün, wenn man den abgesicherten Code löschte?
>
> Je Durchgang eine klassifizierte Liste: blocker / should-fix / minor,
> `datei:zeile`, ein Satz zum Befund, ein Satz zum Szenario. Kein
> Fließtext. Ist ein Bereich sauber, eine Zeile.
>
> Jeden Befund nachstellen, bevor er zur Änderung wird.
