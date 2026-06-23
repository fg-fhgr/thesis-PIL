# Architekturentscheidung: Atomic Fact Extraction Pipeline

**Datum:** 2026-05-26  
**Status:** Bereit zur Implementierung

---

## Context

**Warum diese Änderung?**

Im aktuellen Prototyp (v2) besteht die AI-Pipeline aus zwei getrennten Schritten: CLASSIFY (Sätze einem Bucket zuordnen + Kritikalität vergeben) und ATOMIZE (klassifizierten Text in 6 Textvarianten aufblähen). Dabei wurden folgende Probleme beobachtet:

1. **Kontextverlust bei kern_minimum**: Wenn ein Satz wie "Bei Kindern unter 12 Jahren darf das Mittel nicht eingenommen werden" auf `kern_minimum` verkürzt wird, geht die Population (`Kinder unter 12`) verloren → "Darf nicht eingenommen werden" ohne Kontext, potenziell sicherheitskritisch.
2. **Kein Population-Filtering**: Das Questionnaire fragt dynamisch nach Altersgruppe, Schwangerschaft etc. — aber die aktuellen Atoms haben keine strukturierten Attribute. Eine fact-level Filterung (nur Fakten anzeigen die für diesen User relevant sind) ist unmöglich.
3. **Zwei AI-Schritte ohne klare Trennung**: CLASSIFY und ATOMIZE machen konzeptuell unterschiedliche Dinge, aber die Grenze ist unklar. ATOMIZE arbeitet auf AI-paraphrasiertem Text statt auf Originaltext.
4. **Keine semantischen Metadaten**: Für geplante Icons/Piktogramme (z.B. ⚠️ für Warnungen, ❌ für Kontraindikationen) fehlt ein maschinenlesbares `modality`-Attribut.

**Vorbild:** Google LangExtract zeigt best practice für medizinische Informationsextraktion: strukturierte Attribute (claim, population, condition, modality, negation, source_span) pro Fakt. Für unseren Use Case (ein Medikament pro PIL, UI-Display) adaptiert mit `bucket` (Tile-Zuweisung) und pre-computed Textvarianten.

**Ziel:** Saubere 4-Schichten-Architektur `PARSE → EXTRACT → TRANSFORM → RENDER` mit atomic facts als verlässlicher Basis für Display, Filtering und zukünftige Erweiterungen (Icons, Personalisierung).

---

## Neue Architektur

```
PARSE (Python) → EXTRACT (AI, cached/drug) → TRANSFORM (AI, cached/drug+answers) → RENDER (JS)
```

### Schicht 1: PARSE (unverändert)
`modules/xml_parser.py` → `relevanter_rohtext`

### Schicht 2: EXTRACT (neu — ersetzt CLASSIFY)

**Job:** "Was steht im PIL und was bedeutet es?" — kein Display-Text generieren, nur semantisch extrahieren.

**Caching:** pro Medikament (kein User-Kontext nötig) → `v7_extract_{gtin}`

**Input:** `relevanter_rohtext`

**Output:** Liste von atomic facts

```json
[
  {
    "bucket": "nicht_nehmen",
    "kritikalitaet": 1,
    "claim": "darf nicht eingenommen werden",
    "population": "Kinder unter 12 Jahren",
    "condition": null,
    "modality": "contraindication",
    "negation": true,
    "source_span": "Bei Kindern unter 12 Jahren darf das Mittel nicht eingenommen werden."
  },
  {
    "bucket": "dosierung",
    "kritikalitaet": 2,
    "claim": "Dosis halbieren",
    "population": null,
    "condition": "bei Niereninsuffizienz",
    "modality": "dosage_adjustment",
    "negation": false,
    "source_span": "Bei Niereninsuffizienz sollte die Dosis halbiert werden."
  }
]
```

**Attribut-Definitionen:**

| Attribut | Typ | Beschreibung |
|---|---|---|
| `bucket` | enum | UI-Tile: `wofuer` / `nicht_nehmen` / `dosierung` / `beachten` |
| `kritikalitaet` | 1/2/3 | UI-Sichtbarkeitsfilter (bucket-relativ, kein medizinisches Gefahren-Rating) |
| `claim` | string | Minimale Kernbehauptung, ohne Population/Condition |
| `population` | string\|null | Für wen gilt die Aussage (null = gilt für alle) |
| `condition` | string\|null | Unter welcher Bedingung gilt die Aussage |
| `modality` | enum | `indication` / `contraindication` / `warning` / `dosage_instruction` / `dosage_adjustment` / `side_effect` / `drug_interaction` / `precaution` / `info` |
| `negation` | boolean | Ist die Aussage verneint? |
| `source_span` | string | Exakter Originaltext aus PIL — Nahtlinie zu TRANSFORM |

**Warum `bucket` UND `modality`?**
- `modality` = semantische Bedeutung (für Icons, Safety-Check, maschinelle Verarbeitung)
- `bucket` = UI-Placement (welche Tile)
- Oft ableitbar (contraindication → nicht_nehmen), aber nicht immer eindeutig. AI beurteilt Grenzfälle besser als statische Mapping-Tabelle.

### Schicht 3: TRANSFORM (neu — ersetzt ATOMIZE)

**Job:** "Wie zeigen wir jeden Fakt für diesen User an?" — Display-Text aus `source_span` + semantischen Attributen generieren, plus Relevanz bestimmen.

**Caching:** pro Medikament × Questionnaire-Antworten → `v7_transform_{med_name}|{antworten_json}`

**Input:** atomic facts (aus EXTRACT) + Questionnaire-Antworten

**Output:** Atomic facts angereichert mit Display-Text und `relevant_for_user`

```json
{
  "bucket": "nicht_nehmen",
  "kritikalitaet": 1,
  "claim": "darf nicht eingenommen werden",
  "population": "Kinder unter 12 Jahren",
  "condition": null,
  "modality": "contraindication",
  "negation": true,
  "source_span": "Bei Kindern unter 12 Jahren darf das Mittel nicht eingenommen werden.",
  "relevant_for_user": false,
  "kern_minimum":   { "original": "Kinder unter 12: nicht nehmen.",      "einfach": "Kinder unter 12 dürfen das nicht nehmen." },
  "zusatz_details": { "original": "...",                                  "einfach": "..." },
  "original_satz":  { "original": "Bei Kindern unter 12 Jahren darf ...", "einfach": "..." }
}
```

**`relevant_for_user`:**
- TRANSFORM kennt die Questionnaire-Antworten → kann entscheiden ob ein Fakt für diesen User gilt
- User wählt 18+ → `relevant_for_user: false` für alle Fakten mit `population: "Kinder unter 12 Jahren"`
- Löst das bisherige Problem: Safety-Check war binary (sicher/nicht sicher), jetzt ist es fact-level
- TRANSFORM hat genug Kontext um auch implizite Fälle wie "Personen mit Niereninsuffizienz" korrekt zu beurteilen

**Verbesserung gegenüber heute:**
- TRANSFORM arbeitet aus `source_span` (exakter Originaltext) statt aus AI-paraphrasiertem CLASSIFY-Output
- `kern_minimum` wird aus `claim` + `population` + `condition` generiert → Kontext kann nicht mehr verloren gehen
- "Kinder unter 12: nicht nehmen." statt "Darf nicht eingenommen werden."

### Schicht 4: RENDER (minimal geändert)

```javascript
const visibleFacts = S.atoms
  .filter(f => f.relevant_for_user)          // Population-Filter (neu)
  .filter(f => f.kritikalitaet <= maxKrit);  // Informationsmenge (unverändert)
// Selektion von Textfeld + Sprache: unverändert
```

Icons (für spätere Implementierung vorbereitet):
```javascript
const MODALITY_ICON = {
  'contraindication': '🚫',
  'warning': '⚠️',
  'side_effect': '⚠️',
  'dosage_instruction': '💊',
  'indication': '✅'
};
```

---

## API-Änderungen

| Endpunkt | Vorher | Nachher |
|---|---|---|
| `POST /api/scan` | ruft `_classify_pil_content()` auf → gibt `pil_tile_content` zurück | ruft `_extract_pil_facts()` auf → gibt `atomic_facts` zurück |
| `POST /api/atomize` | ruft `_atomize_pil_content()` auf | ruft `_transform_pil_facts()` auf, Input: `atomic_facts` + Antworten |
| `POST /api/onboarding` | nutzt `pil_fakten` für Fragengenerierung | nutzt `atomic_facts` (strukturierter, AI kann gezielter fragen) |
| `POST /api/safety-check` | nutzt `pil_fakten` + Antworten | nutzt `atomic_facts` mit `modality`/`condition` |
| `GET /api/debug/pipeline/<gtin>` | zeigt rohtext + pil_tile_content | zeigt rohtext + atomic_facts |

---

## Caching-Strategie

| Schicht | Cache-Key | Invalidierung | Kosten |
|---|---|---|---|
| EXTRACT | `v7_extract_{gtin}` | Nie (PIL ändert sich nicht) | Günstig (kein Text generieren) |
| TRANSFORM | `v7_transform_{med_name}\|{antworten_json}` | Neue Antwort-Kombination | Wie heutiges ATOMIZE |

Cache-Version auf `v7_` erhöhen (Breaking Change zum aktuellen v6_-Schema).

---

## Kritische Dateien

| Datei | Änderung |
|---|---|
| `server.py` | `_classify_pil_content()` → `_extract_pil_facts()` neu; `_atomize_pil_content()` → `_transform_pil_facts()` neu; neue Tool-Schemas; `/api/scan` + `/api/atomize` Endpunkte anpassen |
| `CLAUDE.md` | Architektur-Abschnitt komplett ersetzen |
| `ARCHITEKTUR.md` | Neue Schichten-Beschreibung, neues Atom-Datenmodell, neue API-Tabelle |
| `index.html` | Render-Schicht: `relevant_for_user`-Filter hinzufügen |
| `99_Projektdokumentation.md` | Entscheidungseintrag hinzufügen |

---

## Verification

1. `GET /api/debug/pipeline/<gtin>` → zeigt `atomic_facts` mit allen Attributen
2. Stichprobe: Fakt mit `population` prüfen → `kern_minimum` muss Population enthalten
3. Questionnaire mit Altersgruppe 18+ ausfüllen → Canvas darf keine Kinder-spezifischen Fakten zeigen
4. Slider-Interaktion: alle 3 Textlängen × 2 Sprachen × 3 Kritikalitätsstufen funktionieren
5. Cache-Warm-Up: zweiter Scan desselben Medikaments muss sofort antworten (kein AI-Call)
