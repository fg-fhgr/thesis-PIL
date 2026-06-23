# Beipackzettel-Prototyp v2 — Projektinstruktionen

## Projekt
UX-Forschungsprototyp, Masterarbeit FHGR MScUXD.
iPad-Kiosk: Medikament scannen → Beipackzettel laden → KI-transformiert anzeigen.
Studie: 3 Bedingungen (Original-PIL / Statisch optimiert / Nutzerkontrolliert adaptiv).

---

## Architektur (4 Schichten)

```
PARSE → EXTRACT → TRANSFORM → RENDER
```

**Schicht 1 — PARSE** (`modules/xml_parser.py`, deterministisch):
Blacklist-basiert — alles ausser explizit ignorierten Abschnitten wird mitgenommen.
Überschriften als [Marker] erhalten. Output: `relevanter_rohtext` (ein String).

**Schicht 2 — EXTRACT** (`_extract_pil_facts()` in `server.py`, AI, gecacht pro Medikament):
Extrahiert atomic facts mit semantischen Attributen. Kein Display-Text generieren — nur klassifizieren und strukturieren.
`tool_use` + `tool_choice` für Structured Outputs. Gecacht per GTIN → `v9_extract_{gtin}`.
Output: `atomic_facts` → `[{bucket, lese_prioritaet, claim, population, condition, modality, negation, source_span}]`

**Schicht 3 — TRANSFORM** (`_transform_pil_facts()` + `/api/atomize`, AI, gecacht pro Medikament+Antworten):
Deterministischer Filter (P4 entfernen + `_fact_relevant_for_user()` prüft `population`-Feld). LLM generiert Textvarianten pro Fakt.
`relevant_for_user` wird deterministisch bestimmt — kein LLM.
Gecacht per Medikament + Questionnaire-Antworten → `v20_transform_{med_name}|{antworten_json}`.
Output: `atoms` → `[{...EXTRACT-Attribute..., relevant_for_user, kurz, mittel, lang}]`

**Schicht 4 — RENDER** (pure JS, kein API-Call):
Frontend filtert P4-Boilerplate, dann `relevant_for_user`, dann `lese_prioritaet ≤ Schwelle`, dann wählt Textfeld + Sprache.
Slider-Interaktion ist sofort — kein Netzwerk-Request.

---

## Atom-Datenmodell

Jeder Fakt durchläuft zwei Stufen:

**Nach EXTRACT** (semantische Attribute, kein Display-Text):
```json
{
  "bucket": "nicht_nehmen",
  "lese_prioritaet": 1,
  "claim": "darf nicht eingenommen werden",
  "population": "Kinder unter 12 Jahren",
  "condition": null,
  "modality": "contraindication",
  "negation": true,
  "source_span": "Bei Kindern unter 12 Jahren darf das Mittel nicht eingenommen werden."
}
```

**Nach TRANSFORM** (angereichert mit Display-Text + Relevanz):
```json
{
  ...alle EXTRACT-Attribute...,
  "relevant_for_user": false,
  "kurz":   { "default": "...", "einfach": "..." },
  "mittel":  { "default": "...", "einfach": "..." },
  "lang":    { "default": "...", "einfach": "..." }
}
```

**Slider 1 (Informationsmenge):** filtert P4 raus, dann `relevant_for_user`, dann `lese_prioritaet ≤ Schwelle` (1+2+3 / 1+2 / nur 1)
**Slider 2 (Satz-Länge):** wählt Feld (`kurz` / `mittel` / `lang`)
**Toggle (Sprache):** wählt Unter-Feld (`.einfach` / `.default`)

**`lese_prioritaet` — Lese-Priorität (KEIN medizinisches Gefahren-Rating):**
Leitfrage: "Wie dringend muss ein typischer User diese Information in den ersten Sekunden lesen?"

| Prio | Bedeutung | Wofür | Dosierung | Nicht nehmen | Beachten |
|------|-----------|-------|-----------|--------------|---------|
| **P1 — Sofort** | Absolute Essenz (max. 3 Items/Bucket) | Hauptindikation | Maximaldosis, Altersgrenze | Allergie, schwere Organ-Insuffizienz, Schwangerschaft (letztes Trimenon) | Fahrtüchtigkeit, kein Alkohol |
| **P2 — Standard** | Sollte gelesen werden | Weitere Indikationen | Einnahmeregeln, Abstände | Schwangerschaft/Stillzeit, Vorerkrankungen | Häufige NW, bekannte Wechselwirkungen |
| **P3 — Spezial** | Nur für betroffene User | Wirkmechanismus | Einnahme-Tipps | Seltene Warnungen | Seltene Spezial-Wechselwirkungen, Natriumgehalt |
| **P4 — Nie** | Regulatorisches Boilerplate | – | – | – | "Fragen Sie Ihren Arzt", Querverweise, Zulassungshinweise |

**Post-Processing:** Nach dem Chunking-Merging wird P1 per `_calibrate_p1()` auf max. 3 pro Bucket begrenzt. Überschuss wird nach Modality-Priorität auf P2 abgestuft (contraindication/indication behalten P1, precaution/info werden zuerst abgestuft).

---

## 4 Tiles

| Tile-Label | Bucket in `atomic_facts` / `atoms` |
|------------|-------------------------------------|
| Wofür | `wofuer` |
| Nicht nehmen wenn | `nicht_nehmen` |
| Dosierung & Einnahme | `dosierung` |
| Was du beachten solltest | `beachten` |

---

## API-Endpunkte

| Endpunkt | Wann aufgerufen | Wichtige Felder |
|----------|-----------------|-----------------|
| `POST /api/scan` | Barcode erkannt | 1 AI-Call → `relevanter_rohtext` + `atomic_facts` mit `{bucket, lese_prioritaet, claim, population, condition, modality, negation, source_span}` |
| `POST /api/onboarding` | Nach Scan | `atomic_facts` → `questions[]` (kein AI-Call, Keyword-Matching auf `population`) |
| `POST /api/safety-check` | Nach Onboarding | `atomic_facts` + Antworten → `{sicher, typ}` (kein AI-Call, deterministisch) |
| `POST /api/atomize` | Canvas öffnen | `atomic_facts` + Antworten → `atoms` (mit `relevant_for_user` + Textvarianten) |
| `POST /api/generate-all` | Fallback (erhalten) | unverändert |
| `GET /api/debug/pipeline/<gtin>` | Debugging | zeigt rohtext + atomic_facts mit allen Attributen |

---

## Debug & Testen

```
GET https://localhost:5001/api/debug/pipeline/7680563030012
```
Gibt zurück: `relevanter_rohtext` (Parse-Output) + `atomic_facts` mit allen semantischen Attributen (Extract-Output).

Nach `/api/atomize`: `atoms`-Struktur mit `relevant_for_user` + `kurz`/`mittel`/`lang` (je `.default` und `.einfach`) pro Fakt prüfen.

---

## Was funktioniert (nicht anfassen)

- `/api/scan`, `/api/scan_frame`, `/api/generate`, `/api/generate-all` — Fallback-Endpunkte
- Scanner-Screen, Confirmation-Screen, Questionnaire-Screen
- Visuelles Design — Farben, Fonts, Layout, Card-Struktur

---

## Stack

- Flask + HTML/CSS/JS (kein Streamlit, kein React)
- `claude-sonnet-4-6` für Extract + Transform
- `tool_use` + `tool_choice` für Structured Outputs
- Schicht 4 (RENDER): pure JavaScript, kein API-Call

---

## Regeln

- Visuelles Design nicht ändern
- Fallback-Endpunkte erhalten (`/api/generate-all`, `/api/generate`)
- Sprache: Deutsch (UI-Texte, Kommentare)
- Einen Schritt nach dem anderen implementieren, dann testen
