# Prototyp: Nutzergesteuerte Darstellungsadaption medizinischer Packungsbeilagen

UX-Forschungsprototyp zur Masterarbeit (FHGR, MSc UX Design).

Ein iPad-Kiosk-Konzept: Medikament scannen → Beipackzettel (Patient Information Leaflet, PIL)
laden → KI-gestützt transformiert und nutzerkontrolliert anzeigen. Der Prototyp dient dem
Vergleich von drei Aufbereitungs-Bedingungen (Original-PIL / statisch optimiert /
nutzerkontrolliert adaptiv).

---

## Schnellstart (Demo-Modus, ohne iPad)

```bash
# 1. Abhängigkeiten installieren
pip install -r requirements.txt

# 2. API-Key hinterlegen
cp .env.template .env
#    .env öffnen und einen eigenen ANTHROPIC_API_KEY eintragen
#    (https://console.anthropic.com)

# 3. Selbstsigniertes HTTPS-Zertifikat erstellen (einmalig)
brew install mkcert && mkcert -install
mkcert localhost 127.0.0.1
mv localhost+1.pem cert.pem
mv localhost+1-key.pem key.pem

# 4. Server starten
python server.py
#    → https://localhost:5001
```

Im Browser auf `https://localhost:5001` öffnen und den Button **„⚡ Dev: Dafalgan laden"**
nutzen — er überspringt das Scannen und lädt direkt ein Demo-Medikament. Danach den
Onboarding-Fragebogen und die Slider (Informationsmenge / Satzlänge / Sprache) ausprobieren.

> **Hinweis:** Das HTTPS-Zertifikat ist nötig, weil Browser den Kamerazugriff (Scanner)
> nur über HTTPS erlauben. Im reinen Desktop-Demo-Modus reicht ein selbstsigniertes
> Zertifikat. Die ausführliche iPad-Anleitung steht in [`SETUP.md`](SETUP.md).

---

## API-Key & Datenlage

- **`ANTHROPIC_API_KEY` ist erforderlich**, sobald eine nicht zwischengespeicherte
  Kombination aus Medikament und Fragebogen-Antworten erzeugt wird (Schicht 2 & 3 der
  Pipeline rufen Claude auf).
- **Google Gemini ist optional** und wird nur für die Bildgenerierung der
  Narrativ-/Infografik-Ansicht gebraucht.
- **Swissmedic-Daten sind aus Lizenzgründen nicht enthalten.** Der Prototyp läuft für die
  **acht im `cache/`-Ordner hinterlegten Demo-Medikamente** vollständig offline (der
  Scan-Endpunkt liest zuerst den Cache). Das Scannen *neuer* Medikamente ist ohne den
  Swissmedic-Lizenzdatensatz nicht möglich.

Hinterlegte Demo-Medikamente: Dafalgan, Irfen, Pantoprazol-Mepha, Mydocalm,
Tyroqualin, Muco-Mepha, Olfen retard.

---

## Architektur (Kurzfassung)

Vier Schichten: **PARSE → EXTRACT → TRANSFORM → RENDER**

| Schicht | Ort | Aufgabe |
|---------|-----|---------|
| PARSE | `modules/xml_parser.py` | Beipackzettel-Rohtext extrahieren (deterministisch) |
| EXTRACT | `_extract_pil_facts()` in `server.py` (KI, gecacht) | atomare Fakten mit semantischen Attributen |
| TRANSFORM | `_transform_pil_facts()` (KI, gecacht) | Textvarianten + Relevanzfilter pro Fakt |
| RENDER | Frontend `static/index.html` (pures JS) | Slider-/Toggle-Steuerung, kein Netzwerk-Call |

Details: [`ARCHITEKTUR.md`](ARCHITEKTUR.md), [`DESIGN_ATOMIC_FACTS.md`](DESIGN_ATOMIC_FACTS.md)
und [`THESIS_PROTOTYP_BESCHREIBUNG.md`](THESIS_PROTOTYP_BESCHREIBUNG.md).

---

## Projektstruktur

```
server.py            Flask-Server, gesamte API + KI-Pipeline (Schicht 2–3)
config.py            Konfiguration (Keys via Umgebungsvariablen)
modules/             Parser & Barcode-Scanner
static/              Frontend (index.html + UI-Icons)
cache/               vorberechnete KI-Ergebnisse der Demo-Medikamente
icons/referenceImage Referenzbilder für die Infografik-/Narrativ-Ansicht
requirements.txt     Python-Abhängigkeiten
```

---

## Stack

Flask · HTML/CSS/JS (kein Framework) · Anthropic Claude (`claude-sonnet-4-6`) ·
optional Google Gemini für Bildgenerierung.
