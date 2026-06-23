# Prototyp in der Thesis beschreiben — was rein, was raus

Arbeitsnotiz für K4 (Konzept & Prototyp). Geordnet nach: **was unbedingt rein**,
**was spannend/aussergewöhnlich** ist, **was Standard ist** (kurz halten/weglassen)
und **was ehrlich einzuordnen** ist. Jeweils mit kurzer Begründung.
Grundlage: Code (`server.py`, `modules/xml_parser.py`, `static/index.html`), nicht Designnotizen.

---

## A. Muss beschrieben werden (Kern des Artefakts)

1. **Die 4-Schichten-Pipeline PARSE → EXTRACT → TRANSFORM → RENDER**
   *Begründung:* Das ist die zentrale Architekturentscheidung und der rote Faden des
   ganzen Prototyps. Ohne sie ist nichts anderes verständlich. Gehört als Überblick an
   den Anfang von K4.2.

2. **Trennung von Extraktion und Transformation (Schicht 2 vs. 3)**
   *Begründung:* Das ist die eigentliche Designhypothese — "erst klassifizieren/strukturieren,
   dann Text generieren". Sie ist aus der Literatur hergeleitet (Rückbezug K2) und löst
   konkret den Kontextverlust der Vorversion. Das ist der argumentativ stärkste Teil.

3. **Das Atom-Datenmodell (atomic facts mit `bucket`, `lese_prioritaet`, `population`,
   `condition`, `modality`, `negation`, `source_span`)**
   *Begründung:* Es ist die Datengrundlage für *alle* adaptiven Funktionen. Die Slider,
   das Filtering und der Safety-Check sind nur möglich, weil jeder Fakt diese Attribute trägt.
   Datenmodell zeigen (ein JSON-Beispiel reicht).

4. **Die drei Bedienelemente als Operationalisierung von "Nutzerkontrolle"**
   - Slider 1 Satz-Länge → Feldwahl (kurz/mittel/lang)
   - Slider 2 Informationsmenge → `lese_prioritaet`-Schwelle
   - Toggle Sprache → Fachsprache ↔ Einfache Sprache (A2)
   *Begründung:* Das ist die Übersetzung der Forschungsfrage (Nutzerkontrolle/Adaptivität)
   in konkrete Interaktion. Muss erklärt werden, *welche* Dimension welcher Kontrolle entspricht.

5. **Die Studienbedingungen als Artefakt** (Bedingung A = statisch optimiert,
   Bedingung B = nutzerkontrolliert adaptiv; im Code `S.condition`)
   *Begründung:* Der Prototyp *ist* das Stimulusmaterial des Experiments. Wie A und B aus
   derselben Pipeline entstehen (A nutzt feste Felder, kein Fragebogen, keine Slider),
   gehört in K4 — die Methodik/Auswertung selbst nach K5.

---

## B. Spannend / aussergewöhnlich / beeindruckend (hier Substanz zeigen)

6. **Deterministisch ↔ AI sauber getrennt pro Schicht**
   *Begründung:* Ungewöhnlich durchdacht. PARSE und RENDER sind komplett deterministisch,
   nur EXTRACT/TRANSFORM nutzen das LLM — und selbst innerhalb von TRANSFORM sind Relevanz
   und zwei der sechs Textfelder deterministisch berechnet, nicht vom LLM. Das ist ein
   starkes Reproduzierbarkeits-/Kontroll-Argument und hebt sich von "alles in einen Prompt"
   ab. Klar beschreibenswert.

7. **`relevant_for_user` wird deterministisch entschieden, nicht vom LLM** — und *warum*
   *Begründung:* Hier steckt ein echter Erkenntnisgewinn: Das LLM filterte *medizinisch*
   ("Ibuprofen ist für Schwangere nicht relevant") statt *populationsbasiert*, was für die
   Filterlogik falsch war. Die bewusste Entscheidung gegen das LLM (safety-first,
   vorhersehbar) ist genau die Art begründeter Designentscheidung, die eine Thesis sehen will.

8. **Dynamische Onboarding-Fragen aus dem PIL abgeleitet** (`_derive_alter_optionen`,
   `_onboarding_fragen_aus_facts`)
   *Begründung:* Das ist der unterschätzteste, technisch anspruchsvollste Teil. Die
   Altersgruppen-Optionen werden nicht hartcodiert, sondern aus den Fakten *des konkreten
   Medikaments* berechnet — inkl. Aufteilen von Dosierungs-Ranges an Kontraindikations-Grenzen
   (z.B. 6–17 → 6–11 + 12–17, weil es ein "nicht unter 12"-Fakt gibt). Die Schwangerschafts-/
   Stillfrage erscheint nur, wenn der PIL das überhaupt thematisiert. Das ist echte
   inhaltsgetriebene Adaptivität und sehr beschreibenswert.

9. **`lese_prioritaet` als Lese-Priorität, ausdrücklich KEIN medizinisches Gefahren-Rating**
   *Begründung:* Begriffsschärfe, die selten so sauber gezogen wird. Plus die
   `_calibrate_p1()`-Nachkalibrierung (max. 3 P1 pro Bucket, Überschuss nach Modality
   abgestuft) ist eine elegante Antwort auf "das LLM stuft zu viel als wichtig ein".
   Das Spannungsfeld "wie definiert man Wichtigkeit für einen Laien in 5 Sekunden" trägt Substanz.

10. **`source_span` als Nahtstelle / Rückverfolgbarkeit zum Original**
    *Begründung:* Jeder Fakt behält seinen exakten Originalsatz. Das ist die Grundlage dafür,
    dass die "Original"-Sprachstufe wortgetreu bleibt und Transformationen gegen die Quelle
    prüfbar sind — ein Vertrauens-/Sicherheitsargument bei medizinischen Texten.


12. **Drei alternative Render-Paradigmen auf derselben Datengrundlage**
    (Text-Canvas, Narrativ/Comic via `gemini-3-pro-image`, Infografik)
    *Begründung:* Aussergewöhnlich für eine Masterarbeit: Aus *einem* Faktenmodell entstehen
    völlig verschiedene Darstellungsformen (inkl. KI-generierter Comic-Seite, neutral oder
    "Lucky Luke"). Zeigt die Tragfähigkeit der Atom-Abstraktion. *

13. **Blacklist- statt Whitelist-Parsing**
    *Begründung:* Kleine, aber kluge Robustheitsentscheidung — "alles ausser explizit
    Ausgeschlossenem", weil PIL-Strukturen je Medikament variieren. Ein Satz dazu lohnt sich,
    weil es die Generalisierbarkeit über Medikamente hinweg betrifft.


14. **Filterlogik ist bewusst safety-first/grob** (alle unbekannten Populationswerte → relevant).
    *Begründung:* Das ist eine vertretbare, aber konservative Heuristik — eher Stärke
    (zeigt nichts fälschlich aus) als Schwäche. Als bewusste Designentscheidung benennen,
    nicht als Limitation verstecken.
---




---

## D. Ehrlich einordnen (nicht überverkaufen)

21. **Die "Wissenschaftliche Einordnung" in `ARCHITEKTUR.md` vor Verwendung prüfen.**
    *Begründung:* Dort stehen Referenzen wie "Bernicot et al., JMIR 2025 — reduziert
    Halluzinationen um 89 %", "Gutierrez et al., ACL 2025", "Willison 2025",
    "Google LangExtract 2025". Diese sind **nicht in Zotero verifiziert** und teils
    schwer belegbar. Vor jeder Zitation: in Zotero anlegen + Belegstelle lesen, sonst
    nicht zitieren (harte Projektregel). Insbesondere die "89 %"-Zahl nicht ungeprüft
    übernehmen.

22. **3 Bedingungen laut Studie, aber Code implementiert A und B.**
    *Begründung:* CLAUDE.md nennt drei Bedingungen (Original-PIL / statisch optimiert /
    adaptiv); der Prototyp kennt `condition` A (statisch) und B (adaptiv). Die "Original-PIL"-
    Bedingung ist offenbar das ungeschönte Original ausserhalb der Pipeline. Vor dem Schreiben
    klären und konsistent darstellen, damit K4/K5 nicht widersprechen.

23. **Determinismus der Pipeline ist relativ.**
    *Begründung:* PARSE/RENDER sind deterministisch, EXTRACT/TRANSFORM bleiben LLM-Aufrufe —
    also nicht bit-genau reproduzierbar. Ehrlich als "Architektur minimiert die nicht-
    deterministischen Anteile" formulieren, nicht als "deterministisches System".


---

## E. Mechanismen für Korrektheit und Konsistenz des Outputs

Dieser Abschnitt listet alle technischen Massnahmen, die dafür sorgen, dass der Prototyp
**reproduzierbare, korrekte und vollständige** Ausgaben liefert — unabhängig davon, welches
Medikament gescannt wird oder wie oft die Pipeline läuft.


### E2. Structured Outputs via `tool_use` + `tool_choice`

Das LLM wird nicht frei antworten lassen, sondern zwingend in ein vordefiniertes JSON-Schema
gelenkt (`tool_choice={"type": "tool", "name": "..."}`). Alle semantischen Attribute
(`bucket`, `lese_prioritaet`, `claim`, `population`, `modality`, `negation`, `source_span`)
sind als `required` deklariert.

*Funktion:* Das Modell kann kein Feld weglassen, kein Freitext produzieren, keine Struktur
erfinden. Verhindert die häufigste Fehlerquelle bei LLM-Ausgaben: inkonsistente oder
unvollständige Struktur.

### E3. Chunking des PIL-Texts an Abschnittsmarkern

Lange Beipackzettel werden anhand der `[Überschrift]`-Marker aus Schicht 1 in Abschnitte
aufgeteilt, die jeweils separat verarbeitet werden.

*Funktion:* Verhindert, dass der LLM-Output mitten in einem Fakt abbricht (`stop_reason =
"max_tokens"`). Ohne Chunking würden Fakten am Ende langer Abschnitte fehlen.

### E4. Deduplizierung nach `source_span`

Nach dem Chunking werden alle extrahierten Fakten per `source_span`-String verglichen.
Identische Originalsätze, die in mehreren Chunks auftauchen (z.B. weil Abschnitte sich
überlappen), werden entfernt.

*Funktion:* Verhindert doppelte Fakten im Output, ohne Informationen zu verlieren.

### E5. Post-Processing-Kalibrierung in drei Stufen

Nach der LLM-Extraktion durchlaufen alle Fakten drei deterministische Kalibrierungen:

1. **`_calibrate_p1()`** — Max. 3 P1-Fakten pro Bucket. Überschuss wird nach Modality-Priorität
   auf P2 abgestuft (contraindication/indication behalten P1, precaution/info werden zuerst
   abgestuft). Grund: das LLM neigt dazu, zu vieles als "sofort lesen" einzustufen.

2. **`_calibrate_p2()`** — Max. 6 P2-Fakten im `beachten`-Bucket, der erfahrungsgemäss am
   stärksten aufbläht.

3. **`_force_p4_boilerplate()`** — Bestimmte Textmuster (bekannte regulatorische Floskeln)
   werden unabhängig vom LLM-Urteil deterministisch auf P4 gesetzt.

*Funktion:* Das LLM liefert eine grobe Erstklassifizierung; die Kalibrierung setzt enge,
vorhersehbare Grenzen. Kein Beipackzettel kann durch aggressivere LLM-Einschätzung mehr
P1-Fakten produzieren als das System zulässt.

### E6. `lang.default` = `source_span` (deterministisch, kein LLM)

Das längste Textfeld — `lang.default` — ist immer identisch mit dem Originaltext aus dem
Beipackzettel (`source_span`). Es wird nicht generiert, sondern direkt zugewiesen.

*Funktion:* Garantiert, dass die "ungekürzte" Sprachstufe wortgetreu ist. Kein Halluzinations-
risiko, keine Abweichung vom Quelldokument. Gleichzeitig Grundlage dafür, dass Transformationen
gegen die Originalquelle prüfbar bleiben.



### E8. Parallele Chunk-Verarbeitung mit isoliertem Fehler-Handling

In Schicht 3 (TRANSFORM) werden mehrere Fakten-Chunks gleichzeitig verarbeitet
(`ThreadPoolExecutor`). Jeder Chunk hat eigenes Fehler-Handling — ein fehlgeschlagener
Chunk bricht nicht die gesamte Pipeline.

*Funktion:* Robustheit bei grossen Medikamenten-Dossiers. Partielle Fehler führen zu
partiellen Outputs, nicht zu einem Totalausfall.

### E9. `fact_index` als Pflichtfeld im Transform-Schema

Jeder transformierte Fakt muss seinen eigenen Index im Input als `fact_index` zurückliefern
(`required`). Nach der parallelen Verarbeitung werden Ergebnisse über diesen Index den
Original-Fakten zugeordnet.

*Funktion:* Verhindert Zuordnungsfehler bei paralleler Verarbeitung — der Output kann dem
richtigen Fakt zugeordnet werden, auch wenn Chunks in beliebiger Reihenfolge fertig werden.

### E10. Blacklist-Parsing statt Whitelist

Schicht 1 (PARSE) nimmt alles mit, was nicht auf einer expliziten Ausschlussliste steht.
Neue oder unbekannte Abschnitte eines PIL fallen durch in den Output, statt ignoriert zu werden.

*Funktion:* Verhindert stille Informationsverluste bei PIL-Varianten. Ein unbekannter
Abschnitt landet im Rohtext und kann vom LLM eingeordnet werden — statt unsichtbar zu
verschwinden.

### E11. `begruendung_prio` als Pflicht-Begründung vor der Klassifikation

Im Extract-Schema ist `begruendung_prio` ein `required`-Feld, das **vor** `lese_prioritaet`
deklariert ist. Das LLM muss zuerst in natürlicher Sprache begründen, warum ein Fakt eine
bestimmte Priorität verdient — erst dann setzt es den Zahlenwert.

*Funktion:* Erzwingt einen internen Begründungsschritt vor der Entscheidung (Chain-of-Thought
innerhalb des Structured Output). Weil das Modell die Felder sequenziell generiert, wirkt die
ausformulierte Begründung als Selbstkorrektiv: eine schlecht begründete Einschätzung fällt
beim Schreiben auf, bevor sie als Wert festgelegt wird. Verhindert reflexartige
Fehlklassifikationen ohne Argumentationsbasis.

---

**Gesamtbild:** Die Pipeline kombiniert LLM-Flexibilität (Extraktion, Textvarianten) mit
deterministischen Schutzmechanismen (Caching, Kalibrierung, Filterlogik, Structured Outputs).
Kein einzelner Mechanismus garantiert Korrektheit allein — die Robustheit entsteht aus
dem Zusammenspiel aller Schichten.

---

## Vorschlag Gliederung K4.2 (nur als Anker)

- 4.2.1 Überblick: 4-Schichten-Pipeline (Punkt 1, 6)
- 4.2.2 Datengrundlage: Atom-Modell + Lese-Priorität (3, 9, 10)
- 4.2.3 EXTRACT/TRANSFORM getrennt — die zentrale Entscheidung (2, 7)
- 4.2.4 Adaptive Bedienung: Slider, Toggle, Onboarding (4, 8, 11)
- 4.2.5 Studienbedingungen als Artefakt (5, 22)
- (4.2.6 optional: alternative Paradigmen als Exploration — 12)
- Anhang: Endpunkte, Caching, Stack (14–18, 20)
