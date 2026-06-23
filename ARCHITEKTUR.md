# Architektur: KI-Pipeline für Beipackzettel-Transformation

## Problem

Schweizerische Beipackzettel (PILs) sind für Patientinnen und Patienten schwer lesbar.
Der Prototyp transformiert den PIL-Inhalt in vier Informationskacheln (Tiles) und ermöglicht
das Einstellen von Informationsmenge und Satz-Länge via zwei Schiebereglern sowie einem
Sprache-Toggle (Fachsprache ↔ Einfach).

**Ausgangsproblem der ersten Version:**
Das ursprüngliche System schickte den rohen PIL-Text mit einem einzigen Prompt an Claude
mit dem Auftrag, gleichzeitig zu kategorisieren UND zu transformieren. Dies führte zu:

- Falsche Inhalte in falschen Tiles (Vorsichtshinweise im Dosierungs-Tile)
- Fehlende Dosierungsangaben (Trunkierungsbug bei 4000 Zeichen)
- Abschnitte einzelner Medikamente wurden ignoriert (Whitelist-Problem im Parser)
- Slider-Interaktion erforderte API-Calls (Latenz)
- Kontextverlust beim Verdichten: Population/Condition gingen bei kern_minimum verloren
- Kein populationsbasiertes Filtering (Questionnaire-Antworten konnten nicht für fact-level Filterung genutzt werden)

---

## Lösung: 4-Schichten-Architektur

```
[PIL HTML-Datei lokal]
         ↓
┌─────────────────────────────────────────┐
│  SCHICHT 1: PARSE                        │
│  deterministisch, kein AI               │
│  modules/xml_parser.py                  │
│                                         │
│  Output: relevanter_rohtext             │
└──────────────────┬──────────────────────┘
                   ↓
┌─────────────────────────────────────────┐
│  SCHICHT 2: EXTRACT                     │
│  AI, einmalig pro Medikament, gecacht   │
│  _extract_pil_facts() in server.py      │
│                                         │
│  Output: atomic_facts                   │
│  [{bucket, lese_prioritaet, claim,      │
│    population, condition, modality,     │
│    negation, source_span}]              │
└──────────────────┬──────────────────────┘
                   ↓
┌─────────────────────────────────────────┐
│  SCHICHT 3: TRANSFORM                   │
│  AI, einmal pro Med. + Questionnaire    │
│  _transform_pil_facts() + /api/atomize  │
│                                         │
│  Output: atoms                          │
│  [{...EXTRACT-Attribute...,             │
│    relevant_for_user,                   │
│    kern_minimum:   {original, einfach}  │
│    zusatz_details: {original, einfach}  │
│    original_satz:  {original, einfach}  │
│  }]                                     │
└──────────────────┬──────────────────────┘
                   ↓
┌─────────────────────────────────────────┐
│  SCHICHT 4: RENDER                      │
│  deterministisch, pure JavaScript       │
│  renderCanvas() in index.html           │
│                                         │
│  Input:  S.atoms + Slider/Toggle-State  │
│  Output: DOM (4 Tiles)                  │
│  → sofortige Reaktion, kein API-Call    │
└─────────────────────────────────────────┘
```

---

## Schicht 1 — Parse (deterministisch, kein AI)

**Komponente:** `modules/xml_parser.py`

**Prinzip: Blacklist-Ansatz**

Der Parser iteriert alle `<p>`-Elemente des PIL-HTML und prüft für jede Abschnitts-Überschrift,
ob sie auf der Ignore-Liste steht. Steht sie nicht darauf, wird der gesamte Abschnitt mitgenommen.

```
Ignore-Liste (wird verworfen):
  - "Was ist ferner zu beachten"
  - "Was ist in ... enthalten"
  - Zulassungsnummer / Zulassungsinhaber
  - "Wo erhalten Sie..."
  - "Welche Packungen sind erhältlich"
  - "Diese Packungsbeilage wurde ... geprüft"
  - Nebenwirkungen / Unerwünschte Wirkungen
  - "Informationen für Patientinnen und Patienten" (Titelseite)

Alles andere → wird mitgenommen
```

**Warum Blacklist statt Whitelist:**
PILs verschiedener Medikamente haben unterschiedliche Abschnittsstrukturen. Der Blacklist-Ansatz
ist robuster — was nicht explizit ausgeschlossen wird, wird weiterverarbeitet. Die semantische
Zuordnung übernimmt Claude in Schicht 2.

**Überschriften als Kontext-Marker:**
Abschnitts-Überschriften werden als `[Überschrift]`-Marker erhalten. Dies gibt Claude in
Schicht 2 wichtigen Kontext ohne die Klassifizierung daran zu binden.

---

## Schicht 2 — Extract (AI, einmalig gecacht)

**Komponente:** `_extract_pil_facts()` in `server.py`

**Prinzip: Semantische Extraktion — kein Display-Text generieren**

Claude liest den `relevanter_rohtext` und extrahiert jeden Fakt als atomic fact:
1. Ordnet jeden Fakt einem von 4 Tile-Buckets zu (basierend auf Bedeutung, nicht Überschrift)
2. Vergibt eine Lese-Priorität (P1–P4) pro Fakt
3. Extrahiert claim, population, condition, modality, negation und source_span
4. Generiert **keinen** Display-Text — das ist Aufgabe von Schicht 3

**Warum Extraktion und Transformation trennen:**
Wenn ein LLM gleichzeitig extrahiert UND Display-Text generiert, geht bei der Verdichtung
Kontextinformation verloren. Beispiel: "Bei Kindern unter 12 Jahren darf das Mittel nicht
eingenommen werden" → `kern_minimum: "Darf nicht eingenommen werden."` (Population verloren).
Mit strukturierter Extraktion zuerst: `claim: "darf nicht eingenommen werden"` +
`population: "Kinder unter 12 Jahren"` → TRANSFORM kann daraus eine vollständige,
kontexterhaltende kern_minimum generieren.

**Attribut-Definitionen:**

| Attribut | Typ | Beschreibung |
|---|---|---|
| `bucket` | enum | UI-Tile: `wofuer` / `nicht_nehmen` / `dosierung` / `beachten` |
| `lese_prioritaet` | 1/2/3/4 | Lese-Priorität für typischen User (kein medizinisches Gefahren-Rating) |
| `claim` | string | Minimale Kernbehauptung, ohne Population/Condition |
| `population` | string\|null | Für wen gilt die Aussage (null = gilt für alle) |
| `condition` | string\|null | Unter welcher Bedingung gilt die Aussage |
| `modality` | enum | `indication` / `contraindication` / `warning` / `dosage_instruction` / `dosage_adjustment` / `side_effect` / `drug_interaction` / `precaution` / `info` |
| `negation` | boolean | Ist die Aussage verneint? |
| `source_span` | string | Exakter Originaltext aus PIL — Nahtlinie zu Schicht 3 |

**Warum `bucket` UND `modality`:**
- `modality` = semantische Bedeutung (für Icons, Safety-Check, maschinelle Verarbeitung)
- `bucket` = UI-Placement (welche Tile)
- Oft korrelierend (contraindication → nicht_nehmen), aber nicht immer eindeutig. Die AI
  beurteilt Grenzfälle besser als eine statische Mapping-Tabelle.

**Lese-Priorität — UI-Sichtbarkeitsfilter (bucket-relativ):**

Die Priorität ist **kein** medizinisches Gefahren-Rating, sondern beantwortet: *"Wenn der User nur 5 Sekunden hat, um DIESEN Abschnitt zu lesen — wie unverzichtbar ist dieses Item relativ zu den anderen im gleichen Abschnitt?"*

| Priorität | Bedeutung | Wofür | Dosierung | Nicht nehmen | Beachten |
|-----------|-----------|-------|-----------|--------------|---------|
| **P1 — Sofort lesen** | Absolute Essenz (max. 2–3 Items/Abschnitt) | Hauptindikation | Maximaldosis, absolute Altersgrenzen | Häufigste absolute Blocker (Allergie, Schwangerschaft) | Akute Alltagsgefahren (Müdigkeit → kein Autofahren) |
| **P2 — Sollte gelesen werden** | Wichtiger Kontext für die meisten Nutzer | Weitere Indikationen | Normale Einnahmeregel (3× täglich, Dauer) | Spezifische Vorerkrankungen (Niere, Leber) | Wechselwirkungen mit anderen Medikamenten |
| **P3 — Nur für spezielle User** | Nice to know | Wirkmechanismus | Einnahme-Tipps (mit Wasser, nach dem Essen) | Seltene Warnungen | Randnotizen, seltene Komplikationen |
| **P4 — Nie anzeigen** | Regulatorisches Boilerplate ohne Informationswert | — | — | — | Generische "fragen Sie Ihren Arzt"-Sätze, Verweise auf andere Abschnitte, Zulassungsnummern, Hersteller-Kontakt |

**Post-Processing `_calibrate_p1()`:** Nach dem Chunk-Merge werden pro Bucket maximal 3 Fakten als P1 behalten (sortiert nach Modality-Priorität). Überschüssige P1-Fakten werden auf P2 abgewertet.

**Technische Umsetzung:** Anthropics `tool_use` mit `tool_choice`. Das Modell wird gezwungen,
exakt das definierte Schema zu befüllen — valides JSON garantiert.

**Output:** `atomic_facts` — Liste von Fakten mit semantischen Attributen:

```json
[
  {
    "bucket": "nicht_nehmen",
    "lese_prioritaet": 1,
    "claim": "darf nicht eingenommen werden",
    "population": "Kinder unter 12 Jahren",
    "condition": null,
    "modality": "contraindication",
    "negation": true,
    "source_span": "Bei Kindern unter 12 Jahren darf das Mittel nicht eingenommen werden."
  },
  {
    "bucket": "dosierung",
    "lese_prioritaet": 2,
    "claim": "Dosis halbieren",
    "population": null,
    "condition": "bei Niereninsuffizienz",
    "modality": "dosage_adjustment",
    "negation": false,
    "source_span": "Bei Niereninsuffizienz sollte die Dosis halbiert werden."
  }
]
```

**Caching:** `_extract_cache` (Key: `v9_extract_{gtin}`). Einmalig pro Medikament — kein User-Kontext nötig.

---

## Schicht 3 — Transform (AI, gecacht per Med. + Questionnaire)

**Komponente:** `_transform_pil_facts()` + `/api/atomize` in `server.py`

**Prinzip: 4 Schritte — Filter, deterministische Felder, LLM (4 Felder), Atom zusammenbauen**

### Schritt 1 — Deterministischer Filter (kein LLM)

Zwei Filterregeln werden nacheinander angewendet:
1. **P4 entfernen:** Fakten mit `lese_prioritaet == 4` werden nie angezeigt (Boilerplate).
2. **Populationsbasierter Relevanz-Check:** `_fact_relevant_for_user(fact, antworten)` prüft ausschliesslich das `population`-Feld:
   - `population == null` → immer `true` (gilt für alle)
   - `population` enthält "schwanger" → nur `true` wenn User schwanger
   - `population` enthält "still" → nur `true` wenn User stillt
   - `population` mit Altersangabe → numerischer Overlap-Check mit User-Altersgruppe
   - Alle anderen Populationswerte → `true` (safety-first)
   - Das `condition`-Feld wird bewusst **nicht** gefiltert — Vorerkrankungen sind userrelevant

**Warum deterministisch statt LLM:**
Ein LLM interpretiert medizinisch statt populationsbasiert — es hat z.B. bei Ibuprofen alle `wofuer`-Fakten als nicht-relevant für Schwangere markiert (korrekt medizinisch, falsch für unsere Filterlogik). Die deterministische Lösung ist vorhersehbar und safety-first.

### Schritt 2 — Deterministische Felder (kein LLM)

Zwei Felder werden direkt aus den EXTRACT-Attributen abgeleitet — kein LLM nötig:

**`kern_minimum.original`** — via `_build_kern_minimum_original(fact)`:
```python
# population + condition als Prefix vor claim
# "Kinder unter 12 Jahren" + None + "nicht nehmen" → "Kinder unter 12 Jahren: nicht nehmen."
# None + "bei Niereninsuffizienz" + "Dosis halbieren" → "bei Niereninsuffizienz: Dosis halbieren."
# None + None + "schmerzstillend" → "schmerzstillend."
```

**`original_satz.original`** = `source_span` aus EXTRACT (1:1, kein LLM)

### Schritt 3 — LLM generiert 4 Felder pro Fakt

**LLM-Input pro Fakt (minimal — nur claim + source_span):**
```
[0]
claim: nicht einnehmen bei Allergie gegen Inhaltsstoffe
source_span: Irfen darf nicht eingenommen werden, wenn Sie auf einen der Inhaltsstoffe...
```

Population, Condition und Modality sind im LLM-Input nicht enthalten — der claim enthält bereits die Kernaussage, source_span liefert den Kontext.

**LLM-Output: 4 flache Strings pro Fakt**

| Feld | Inhalt | Herkunft |
|------|--------|----------|
| `kern_minimum_einfach` | Kernaussage A2, max. 10 Wörter | LLM |
| `zusatz_details_original` | claim + wichtigster Kontext aus source_span, 2–3 Sätze, nur Originalwörter | LLM |
| `zusatz_details_einfach` | gleicher Inhalt wie zusatz_details_original, aber A2-Niveau | LLM |
| `original_satz_einfach` | source_span vollständig auf A2 umgeschrieben, nichts weglassen | LLM |

**Technisch:** Anthropic `tool_use` + `tool_choice`, chunk_size=10, max_workers=3, max_tokens=6144. Parallel via `concurrent.futures.ThreadPoolExecutor`.

### Schritt 4 — Atom zusammenbauen

6 Textvarianten werden aus den Schritten 2 + 3 zusammengesetzt:

```
kern_minimum.original    ← deterministisch (Schritt 2)
kern_minimum.einfach     ← LLM (Schritt 3)
zusatz_details.original  ← LLM (Schritt 3)
zusatz_details.einfach   ← LLM (Schritt 3)
original_satz.original   ← deterministisch (Schritt 2, = source_span)
original_satz.einfach    ← LLM (Schritt 3)
```

**Das Atom-Datenmodell nach TRANSFORM:**

```json
{
  "bucket": "nicht_nehmen",
  "lese_prioritaet": 1,
  "claim": "darf nicht eingenommen werden",
  "population": "Kinder unter 12 Jahren",
  "condition": null,
  "modality": "contraindication",
  "negation": true,
  "source_span": "Bei Kindern unter 12 Jahren darf das Mittel nicht eingenommen werden.",

  "relevant_for_user": false,

  "kern_minimum": {
    "original": "Kinder unter 12 Jahren: nicht nehmen.",
    "einfach":  "Kinder unter 12 dürfen das nicht nehmen."
  },
  "zusatz_details": {
    "original": "Das Mittel darf bei Kindern unter 12 Jahren nicht eingenommen werden — Dosierung und Sicherheit sind für diese Altersgruppe nicht belegt.",
    "einfach":  "Für Kinder unter 12 ist das Mittel nicht geeignet, da es für sie nicht getestet wurde."
  },
  "original_satz": {
    "original": "Bei Kindern unter 12 Jahren darf das Mittel nicht eingenommen werden.",
    "einfach":  "Wenn dein Kind unter 12 Jahre alt ist, darf es dieses Mittel nicht nehmen."
  }
}
```

**`relevant_for_user`:**
- Nur Fakten mit `true` werden in den Output-Buckets gespeichert (nicht relevante werden bereits in Schritt 1 herausgefiltert)
- Alle Atoms in `S.atoms` haben implizit `relevant_for_user: true`

**Felddefinitionen:**

| Feld | Slider-Position | Herkunft |
|------|-----------------|----------|
| `kern_minimum` | Satz-Länge kurz (Slider-Wert 0) | .original deterministisch, .einfach LLM |
| `zusatz_details` | Satz-Länge mittel (Slider-Wert 2) | beide LLM |
| `original_satz` | Satz-Länge lang (Slider-Wert 4, **Default**) | .original = source_span direkt, .einfach LLM |

**Caching:** `_transform_cache` (Key: `v13_transform_{med_name}|{antworten_json}`).

**Output:** `atoms` — alle Fakten angereichert mit Display-Text und `relevant_for_user`.

---

## Schicht 4 — Render (deterministisch, pure JavaScript)

**Komponente:** `renderCanvas()` in `static/index.html`

**Prinzip: Feldauswahl aus vorberechneten Atoms**

Schicht 4 ist kein API-Call — sie liest aus `S.atoms` das richtige Feld basierend auf dem
aktuellen Slider/Toggle-Zustand:

```javascript
const SATZ_FELDER   = {0: 'kern_minimum', 2: 'zusatz_details', 4: 'original_satz'};
const KRIT_SCHWELLE = {0: 3, 2: 2, 4: 1};

function renderCanvas() {
  const satzFeld = SATZ_FELDER[S.satzlaenge];
  const langFeld = S.sprache === 0 ? 'original' : 'einfach';
  const maxKrit  = KRIT_SCHWELLE[S.informationsmenge];

  TILES.forEach(tile => {
    const atoms = S.atoms[tile.key]
      .filter(a => a.relevant_for_user)              // Population-Filter
      .filter(a => a.lese_prioritaet <= maxKrit);   // Informationsmenge-Filter (P4 bereits frontend-seitig entfernt)
    const texte = atoms.map(a => a[satzFeld][langFeld]);
    renderTile(tile, texte);
  });
}
```

**Slider-Interaktion:** `onSliderChange()` ruft direkt `renderCanvas()` auf — synchron, kein await,
kein Netzwerk-Request. Sofortige DOM-Aktualisierung.

**UI-Controls:**
- Toggle (Sprache): Original / Einfach → `S.sprache` (0/1)
- Slider 1 (Informationsmenge): Alle / Standard / Nur Wichtiges → `S.informationsmenge` (0/2/4)
- Slider 2 (Satz-Länge): Ultrakurz / Ausführlich / Original → `S.satzlaenge` (0/2/4)

---

## Datenfluss im Gesamtsystem

```
Nutzer scannt Barcode
    ↓
/api/scan
    ↓ parse_beipackzettel() → relevanter_rohtext            [Schicht 1]
    ↓ _extract_pil_facts()  → atomic_facts                  [Schicht 2]
    → Response: { medikament_name, relevanter_rohtext, atomic_facts }
    (pil_rohtext: Legacy-Struct für Fallback-Endpunkte /api/generate, /api/generate-all)

Nutzer beantwortet Onboarding-Fragen
    ↓
/api/onboarding
    ↓ atomic_facts → keyword-basiertes Matching → questions[] (kein AI-Call)

/api/safety-check
    ↓ atomic_facts + antworten → { sicher, typ, nachricht }

Nutzer gelangt zum Canvas-Screen
    ↓
/api/atomize                                                 [Schicht 3]
    ↓ _transform_pil_facts(atomic_facts, antworten)
    → { atoms: { wofuer: [...], nicht_nehmen: [...], dosierung: [...], beachten: [...] } }

Slider/Toggle-Interaktion
    → sofort: renderCanvas() liest S.atoms, kein API-Call   [Schicht 4]
    → filtert P4-Boilerplate, dann relevant_for_user, dann lese_prioritaet ≤ Schwelle; wählt Textfeld + Sprache
```

---

## Caching-Strategie

| Cache | Schlüssel | Inhalt | Lebensdauer |
|-------|-----------|--------|-------------|
| `_pil_cache` | GTIN | PIL-Rohtext | Server-Laufzeit |
| `_extract_cache` | `v9_extract_{gtin}` | atomic_facts mit semantischen Attributen | Server-Laufzeit |
| `_transform_cache` | `v13_transform_{med}\|{antworten}` | Enriched atoms (6 Textfelder + relevant_for_user, 2 deterministisch + 4 LLM) | Server-Laufzeit |
| Disk-Cache | GTIN.json | atomic_facts (bei Schema-Änderung löschen) | Dauerhaft |

---

## API-Endpunkte

| Endpunkt | Methode | Wann | Input → Output |
|----------|---------|------|----------------|
| `/api/scan` | POST | Barcode erkannt | barcode → medikament_name, relevanter_rohtext, atomic_facts (1 AI-Call) |
| `/api/onboarding` | POST | Nach Scan | atomic_facts → questions[] (kein AI-Call, Keyword-Matching) |
| `/api/safety-check` | POST | Nach Onboarding | atomic_facts + antworten → {sicher, typ} (kein AI-Call) |
| `/api/atomize` | POST | Canvas öffnen | atomic_facts + antworten → atoms |
| `/api/generate-all` | POST | Fallback (erhalten) | pil_tile_content → results {0_0..4_4} |
| `/api/generate` | POST | Fallback (erhalten) | PIL-Rohtext → result |
| `/api/debug/pipeline/<gtin>` | GET | Debugging | → rohtext + atomic_facts mit allen Attributen |

---

## Wissenschaftliche Einordnung

Die 4-Schichten-Architektur folgt dem Prinzip der Aufgabentrennung bei LLM-Pipelines:

**Two-Phase LLM Framework** (Bernicot et al., JMIR Medical Informatics, 2025):
Trennung von Extraktion und Verarbeitung reduziert Halluzinationen um 89%.

**"LLMs are not Zero-Shot Reasoners for Biomedical IE"** (Gutierrez et al., ACL 2025):
Schema-getriebene Extraktion ist der robusteste Ansatz für medizinische Texte.

**Structured Data Extraction via LLM Schemas** (Willison, 2025):
Feldbeschreibungen als Fragen formuliert verbessern die Klassifizierungsgenauigkeit.

**Atomic Fact Extraction** (Min et al., FactScore, 2023):
Zerlegung komplexer Sätze in minimale, unabhängig verifizierbare Informationseinheiten
ermöglicht präzise Kontrolle über Informationsdichte — Grundlage für Slider 1.

**LangExtract / Information Extraction** (Google, 2025):
Strukturierte Attribute (claim, population, condition, modality, source_span) pro Fakt
als best practice für medizinische IE. Für unseren Single-Drug-PIL-Use-Case adaptiert
mit `bucket` (UI-Placement) und pre-computed Textvarianten.

---

## Technologie-Entscheide

| Entscheid | Gewählt | Begründung |
|-----------|---------|------------|
| Structured Outputs | Anthropic `tool_use` | Nativ, keine neue Dependency, garantiertes Schema |
| Lese-Priorität | 4-stufig (P1–P4, P4 = nie anzeigen) | P4 ermöglicht Boilerplate-Filterung; P1-Kalibrierung via `_calibrate_p1()` |
| Slider-Rendering | Pure JS (kein API-Call) | Sofortige Reaktion, keine Latenz |
| Extract + Transform | 2 getrennte Schritte | Kontexterhalt, weniger Fehler, klare Verantwortung pro Schicht |
| Modell | claude-sonnet-4-6 | Zuverlässige strukturierte Extraktion + Transformation |
