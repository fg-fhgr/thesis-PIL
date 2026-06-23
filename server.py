"""
server.py — Flask Backend für Beipackzettel-Prototyp v2
Start: python server.py
iPad: https://<MacBook-IP>:5001
"""

import json
import re
import sys
import os
import concurrent.futures
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

sys.path.insert(0, str(Path(__file__).parent))
import config
from modules.xml_parser import parse_beipackzettel
from modules.barcode_scanner import (
    eintrag_fuer_barcode_string,
    eintrag_dynamisch_laden,
    gtin_zu_swissmedic_id,
)

import anthropic

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

def _clean_med_name(name: str) -> str:
    """Entfernt Zulassungsnummer-Klammern aus dem Medikamentennamen."""
    return re.sub(r'\s*\(Zul\.?-?Nr\.?[.:]?\s*\d+\)', '', name).strip()


# ── Cache ────────────────────────────────────────────────────────────────
_SCAN_CACHE_DIR = Path(__file__).parent / "cache"
_SCAN_CACHE_DIR.mkdir(exist_ok=True)
_TRANSFORM_CACHE_DIR = Path(__file__).parent / "cache" / "transform"
_TRANSFORM_CACHE_DIR.mkdir(exist_ok=True)

_pil_cache = {}        # gtin → pil_daten (in-memory)
_extract_cache = {}    # v23_extract_{md5} → atomic_facts
_transform_cache = {}  # v21_transform_{med_name}|{antworten_json} → atoms_by_bucket
_onboarding_cache = {} # cache_key → fragen


def _scan_cache_load(gtin: str) -> dict | None:
    """Lädt gecachten Scan-Result vom Disk. Gibt None zurück wenn nicht vorhanden."""
    path = _SCAN_CACHE_DIR / f"{gtin}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _scan_cache_save(gtin: str, result: dict) -> None:
    """Speichert Scan-Result auf Disk."""
    path = _SCAN_CACHE_DIR / f"{gtin}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def _transform_cache_load(cache_key: str) -> dict | None:
    """Lädt gecachten Transform-Result vom Disk."""
    import hashlib
    fname = hashlib.md5(cache_key.encode()).hexdigest() + ".json"
    path = _TRANSFORM_CACHE_DIR / fname
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("cache_key") == cache_key:
            return data.get("buckets")
    return None


def _transform_cache_save(cache_key: str, buckets: dict) -> None:
    """Speichert Transform-Result auf Disk."""
    import hashlib
    fname = hashlib.md5(cache_key.encode()).hexdigest() + ".json"
    path = _TRANSFORM_CACHE_DIR / fname
    path.write_text(json.dumps({"cache_key": cache_key, "buckets": buckets}, ensure_ascii=False, indent=2), encoding="utf-8")

# ── Tool-Schemas für Structured Outputs (tool_use) ─────────────────────────
GENERATE_TOOL = {
    "name": "generate_tile_content",
    "description": "Transformiert PIL-Tile-Inhalte nach Textlänge und Sprachkomplexität.",
    "input_schema": {
        "type": "object",
        "properties": {
            "wofuer": {
                "type": "object",
                "properties": {"punkte": {"type": "array", "items": {"type": "string"}}},
                "required": ["punkte"]
            },
            "nicht_nehmen": {
                "type": "object",
                "properties": {"punkte": {"type": "array", "items": {"type": "string"}}},
                "required": ["punkte"]
            },
            "dosierung": {
                "type": "object",
                "properties": {
                    "schritte": {"type": "array", "items": {"type": "string"}},
                    "max_dauer": {"type": ["string", "null"]}
                },
                "required": ["schritte", "max_dauer"]
            },
            "beachten": {
                "type": "object",
                "properties": {
                    "punkte": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "typ": {"type": "string", "enum": ["warnung", "info"]}
                            },
                            "required": ["text", "typ"]
                        }
                    }
                },
                "required": ["punkte"]
            },
            "narrativ_panels": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sektion": {"type": "string"},
                        "story_text": {"type": "string"},
                        "bild_prompt": {"type": "string"}
                    },
                    "required": ["sektion", "story_text", "bild_prompt"]
                }
            },
            "vereinfachungen": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "original": {"type": "string"},
                        "vereinfacht": {"type": "string"}
                    },
                    "required": ["original", "vereinfacht"]
                }
            }
        },
        "required": ["wofuer", "nicht_nehmen", "dosierung", "beachten", "narrativ_panels", "vereinfachungen"]
    }
}

_ATOM_TEXT_PAIR = {
    "type": "object",
    "properties": {
        "original": {"type": "string"},
        "einfach":  {"type": "string"}
    },
    "required": ["original", "einfach"]
}

_ATOM_SCHEMA = {
    "type": "object",
    "properties": {
        "kritikalitaet":  {"type": "integer", "enum": [1, 2, 3]},
        "kern_minimum":   _ATOM_TEXT_PAIR,
        "zusatz_details": _ATOM_TEXT_PAIR,
        "original_satz":  _ATOM_TEXT_PAIR,
    },
    "required": ["kritikalitaet", "kern_minimum", "zusatz_details", "original_satz"]
}

ATOMIZE_TOOL = {
    "name": "atomize_pil",
    "description": "Reichert klassifizierte PIL-Fakten mit allen 6 Textvarianten an (3 Satz-Längen × 2 Sprachen).",
    "input_schema": {
        "type": "object",
        "properties": {
            "wofuer":       {"type": "array", "items": _ATOM_SCHEMA},
            "nicht_nehmen": {"type": "array", "items": _ATOM_SCHEMA},
            "dosierung":    {"type": "array", "items": _ATOM_SCHEMA},
            "beachten":     {"type": "array", "items": _ATOM_SCHEMA},
        },
        "required": ["wofuer", "nicht_nehmen", "dosierung", "beachten"]
    }
}

# ── Schicht 2: EXTRACT — Tool-Schema ───────────────────────────────────────
_ATOMIC_FACT_SCHEMA = {
    "type": "object",
    "properties": {
        "begruendung_prio": {
            "type": "string",
            "description": (
                "Denke Schritt für Schritt BEVOR du bucket und lese_prioritaet vergibst: "
                "(1) Wäre dieser Satz sinngleich in JEDEM Beipackzettel (Paracetamol, Ibuprofen, Antibiotikum)? → Dann P4. "
                "(2) Falls nein: Hauptindikation, Maximaldosis, häufigste absolute Kontraindikation oder akute Alltagsgefahr? → P1. "
                "(3) Sollte gelesen werden, aber keine unmittelbare Gefahr? → P2. "
                "(4) Nur für spezielle Untergruppen, Wirkmechanismus, seltene Ausnahmen? → P3. "
                "Schreibe deine Begründung in 1–2 Sätzen. Erst dann vergib bucket und lese_prioritaet."
            )
        },
        "bucket": {
            "type": "string",
            "enum": ["wofuer", "nicht_nehmen", "dosierung", "beachten"],
            "description": (
                "UI-Tile. wofuer=Wofür wird es angewendet; "
                "nicht_nehmen=Wann darf es nicht eingenommen werden; "
                "dosierung=Wie viel, wie oft, wie lange; "
                "beachten=Wechselwirkungen, Warnhinweise, besondere Situationen"
            )
        },
        "lese_prioritaet": {
            "type": "integer",
            "enum": [1, 2, 3, 4],
            "description": (
                "Lese-Priorität für einen typischen User. KEIN medizinisches Gefahren-Rating. "
                "P1 — Sofort lesen: Hauptindikation | Maximaldosis + Altersgrenze | häufigste absolute Kontraindikation (Allergie, schwere Leber-/Niereninsuffizienz, Schwangerschaft letztes Trimenon) | akute Alltagsgefahr (Fahrtüchtigkeit, kein Alkohol). "
                "P2 — Sollte gelesen werden: Häufige Nebenwirkungen | Einnahmeregeln | Schwangerschaft/Stillzeit | bekannte Wechselwirkungen (Blutverdünner, andere NSAIDs). "
                "P3 — Nur für spezielle User: seltene Wechselwirkungen mit Spezialmitteln | Wirkmechanismus | Natriumgehalt | Lagerungshinweise. "
                "P4 — Nie anzeigen (regulatorisches Boilerplate): "
                "ZWINGEND P4: generische 'fragen Sie Ihren Arzt/Apotheker'-Sätze ohne konkreten med. Inhalt | "
                "Verweise auf andere Abschnitte ('siehe Abschnitt X') | Zulassungsnummern | Hersteller-Kontakt | "
                "rein formale Anweisungen ohne Handlungsbezug ('dieses Arzneimittel wurde Ihnen persönlich verschrieben') | "
                "generische Dosierungsformeln die für jedes Medikament gelten: "
                "'Überschreiten Sie nicht die empfohlene Dosis', 'Nehmen Sie das Arzneimittel genau nach Anweisung' — "
                "Prüftest: Steht dieser Satz wörtlich in JEDEM Beipackzettel? → P4."
            )
        },
        "claim": {
            "type": "string",
            "description": (
                "Minimale Kernbehauptung — NUR die Aussage selbst, OHNE Population und Condition. "
                "Falsch: 'Kinder unter 12 Jahren dürfen nicht nehmen'. "
                "Richtig: 'darf nicht eingenommen werden' (Population separat)."
            )
        },
        "population": {
            "type": ["string", "null"],
            "description": (
                "Für wen gilt die Aussage. Null wenn für alle Personen. "
                "Altersangaben IMMER mit konkreter Jahreszahl — auch wenn der PIL nur 'Erwachsene' schreibt: "
                "'Kinder 9–12 Jahre', 'Erwachsene ab 18 Jahren', 'Kinder und Jugendliche ab 6 Jahren', "
                "'Säuglinge unter 1 Jahr', 'ältere Patienten ab 65 Jahren'. "
                "Standard-Mappings: 'Erwachsene' → 'Erwachsene ab 18 Jahren'; "
                "'Jugendliche' (ohne Zahl) → 'Jugendliche 12–17 Jahre'; "
                "'Kinder' (ohne Zahl) → 'Kinder unter 12 Jahren'. "
                "Nicht-Alters-Beispiele: 'Schwangere', 'Personen mit Niereninsuffizienz'."
            )
        },
        "condition": {
            "type": ["string", "null"],
            "description": (
                "Unter welcher Bedingung gilt die Aussage. Null wenn bedingungslos. "
                "Beispiele: 'bei gleichzeitiger Einnahme von Aspirin', 'nach dem Essen'"
            )
        },
        "modality": {
            "type": "string",
            "enum": [
                "indication",
                "contraindication",
                "warning",
                "dosage_instruction",
                "dosage_adjustment",
                "side_effect",
                "drug_interaction",
                "precaution",
                "info"
            ],
            "description": (
                "Semantische Art der Aussage. "
                "indication=Anwendungsgebiet; contraindication=absolutes Verbot; "
                "warning=Vorsichtshinweis; dosage_instruction=Standarddosierung; "
                "dosage_adjustment=Dosisanpassung für bestimmte Gruppen; "
                "side_effect=Nebenwirkung; drug_interaction=Wechselwirkung; "
                "precaution=Vorsichtsmassnahme (Autofahren, Alkohol); info=allgemeine Information"
            )
        },
        "negation": {
            "type": "boolean",
            "description": "True wenn die Aussage etwas verbietet oder verneint (darf nicht, soll nicht, nicht empfohlen)"
        },
        "source_span": {
            "type": "string",
            "description": (
                "Exakter Originaltext aus dem PIL — vollständiger Satz oder zusammenhängende Satzgruppe. "
                "Kontextabhängige Folgesätze ('daher', 'deshalb', 'dies', 'diese') "
                "mit ihrem Vorläufer-Satz zusammenfassen."
            )
        }
    },
    "required": ["begruendung_prio", "bucket", "lese_prioritaet", "claim", "population", "condition", "modality", "negation", "source_span"]
}

EXTRACT_TOOL = {
    "name": "extract_pil_facts",
    "description": "Extrahiert alle relevanten Fakten aus dem PIL als strukturierte atomic facts mit semantischen Attributen. Kein Display-Text — nur klassifizieren und strukturieren.",
    "input_schema": {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": _ATOMIC_FACT_SCHEMA,
                "description": "Vollständige Liste aller extrahierten atomic facts aus dem PIL"
            }
        },
        "required": ["facts"]
    }
}

# ── Static ─────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/icons/<path:filename>")
def serve_icons(filename):
    if filename.lower().endswith(".svg"):
        filepath = os.path.join("icons", filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            # USP-Piktogramme sind hochformatig (Höhe > Breite × 1.04).
            # Der untere Bereich enthält Beschriftungstext als Vektorpfade.
            # Crop: viewBox-Höhe auf Breite × 0.965 kürzen → Text ausgeblendet.
            # DHIS2-Icons sind quadratisch (ratio=1.0) → werden nicht verändert.
            def _crop_usp_text(m):
                parts = m.group(1).split()
                if len(parts) == 4:
                    try:
                        x, y, w, h = (float(p) for p in parts)
                        if h > w * 1.04:
                            new_h = round(w * 0.965, 2)
                            return f'viewBox="{int(x)} {int(y)} {int(w)} {new_h}"'
                    except ValueError:
                        pass
                return m.group(0)
            content = re.sub(r'viewBox="([^"]*)"', _crop_usp_text, content)
            return Response(content, mimetype="image/svg+xml",
                            headers={"Cache-Control": "max-age=3600"})
        except OSError:
            pass
    return send_from_directory("icons", filename)

# ── API: Scan ──────────────────────────────────────────────────────────────
@app.route("/api/scan", methods=["POST"])
def api_scan():
    data = request.get_json()
    gtin = (data.get("barcode") or data.get("gtin") or "").strip()

    if not gtin:
        return jsonify({"success": False, "error": "Kein Barcode angegeben"}), 400

    # In-Memory Cache
    if gtin in _pil_cache:
        return jsonify({"success": True, **_pil_cache[gtin]})

    # Disk-Cache (überlebt Server-Neustarts)
    cached = _scan_cache_load(gtin)
    if cached:
        _pil_cache[gtin] = cached
        return jsonify({"success": True, **cached})

    # GTIN → Swissmedic-ID → Mapping-Eintrag
    swissmedic_id = gtin_zu_swissmedic_id(gtin)
    if not swissmedic_id:
        return jsonify({"success": False, "error": "Medikament nicht gefunden. Bitte Barcode nochmals scannen."}), 404

    eintrag = eintrag_fuer_barcode_string(gtin)
    if not eintrag:
        eintrag = eintrag_dynamisch_laden(swissmedic_id)

    if not eintrag:
        return jsonify({"success": False, "error": "Kein Beipackzettel gefunden für dieses Medikament."}), 404

    lokale_datei = eintrag.get("lokale_datei")
    if not lokale_datei:
        return jsonify({"success": False, "error": "Beipackzettel-Datei nicht verfügbar."}), 404

    pil = parse_beipackzettel(lokale_datei)
    relevanter_rohtext = pil.get("relevanter_rohtext", "")
    pil_rohtext = {
        "kurzfassung": pil.get("kurzfassung_roh", ""),
        "warnhinweise": pil.get("warnhinweise_roh", ""),
        "dosierung":    pil.get("dosierung_roh", ""),
    }
    med_name = _clean_med_name(eintrag.get("beschreibung", pil.get("medikament", "Unbekannt")))
    result = {
        "medikament_name":    med_name,
        "relevanter_rohtext": relevanter_rohtext,
        "pil_rohtext":        pil_rohtext,
        "pil_fakten":         None,
        "pil_tile_content":   None,
        "atomic_facts":       _extract_pil_facts(relevanter_rohtext, med_name),
    }
    _pil_cache[gtin] = result
    _scan_cache_save(gtin, result)
    return jsonify({"success": True, **result})


# ── API: Scan Frame (Fallback für Browser ohne BarcodeDetector) ─────────────
@app.route("/api/scan_frame", methods=["POST"])
def api_scan_frame():
    """Dekodiert einen Barcode aus einem Kamera-Frame (pyzbar Fallback)."""
    if "frame" not in request.files:
        return jsonify({"success": False, "error": "Kein Frame-Bild empfangen"}), 400
    frame_bytes = request.files["frame"].read()
    try:
        from modules.barcode_scanner import barcode_aus_bild_lesen
        barcode = barcode_aus_bild_lesen(frame_bytes)
    except Exception:
        barcode = None
    if not barcode:
        return jsonify({"success": False, "error": "Kein Barcode erkannt"})
    return jsonify({"success": True, "barcode": barcode})



# ── PIL-Extraktion (Schicht 2: Extract) ────────────────────────────────────
def _extract_pil_facts(relevanter_rohtext: str, med_name: str) -> list:
    """Schicht 2: Extrahiert atomic facts mit semantischen Attributen. Einmalig pro Medikament gecacht.
    Teilt langen PIL-Text in Abschnitts-Chunks auf um max_tokens-Limit zu vermeiden."""
    import hashlib
    cache_key = f"v23_extract_{hashlib.md5(relevanter_rohtext.encode()).hexdigest()}"
    if cache_key in _extract_cache:
        return _extract_cache[cache_key]

    system = (
        "Du bist ein medizinischer Informationsextraktor für einen UX-Forschungsprototyp. "
        "Lies den Beipackzettel-Text und extrahiere jeden relevanten Satz als atomic fact. "
        "Ignoriere die eckigen Klammer-Überschriften [so] — sie sind nur Kontext-Hinweise. "
        "Klassifiziere rein nach dem INHALT, nicht nach der Überschrift.\n\n"
        "CLAIM-REGEL: Der claim ist die minimale Behauptung ohne Population und Condition — "
        "der claim muss auch ohne sie noch vollständig Sinn ergeben.\n"
        "Trenne sie explizit heraus:\n"
        "  Falsch: claim='Kinder unter 12 Jahren dürfen nicht nehmen'\n"
        "  Richtig: claim='darf nicht eingenommen werden', population='Kinder unter 12 Jahren'\n"
        "Gilt auch für Handlungsaufforderungen — claim = minimales Verb, condition = der Auslöser:\n"
        "  Falsch: claim='Arzt konsultieren bei hohem Fieber oder Symptomen über 3 Tage'\n"
        "  Richtig: claim='Arzt konsultieren', condition='bei hohem Fieber oder Symptomen über 3 Tage'\n\n"
        "CLAIM-QUALITÄTS-REGEL: Der claim muss den konkreten Inhalt enthalten. "
        "Verboten als alleiniger claim: 'Vorsicht angezeigt', 'Arzt informieren', 'Beachten'. "
        "Der claim muss das Risiko oder die Konsequenz nennen:\n"
        "  Falsch: claim='Vorsicht angezeigt', population='Patienten mit Asthma'\n"
        "  Richtig: claim='kann Bronchospasmen auslösen', population='Patienten mit Asthma'\n"
        "  Falsch: claim='Vorsicht angezeigt', population='Patienten mit Magengeschwür-Vorgeschichte'\n"
        "  Richtig: claim='erhöhtes Risiko für Magen-Darm-Blutungen', population='Patienten mit Magengeschwür-Vorgeschichte'\n\n"
        "INDIKATIONS-VOLLSTÄNDIGKEITS-REGEL: Wenn der Beipackzettel mehrere Anwendungsgebiete auflistet, "
        "müssen ALLE in einem Fakt erfasst werden — kein Weglassen.\n"
        "  Falsch: claim='zur Behandlung von Kopfschmerzen'  [Rest der Liste ignoriert]\n"
        "  Richtig: claim='zur Behandlung von Kopf-, Zahn-, Rücken- und Regelschmerzen, Arthrose sowie Fieber'\n"
        "           source_span=vollständiger Indikationsabschnitt\n"
        "  Wenn die Hauptgruppe bereits im P1-Fakt steht (z.B. 'schmerzlindernd und fiebersenkend'),\n"
        "  können weitere spezifische Indikationen als P2-Fakt erscheinen — aber kein Löschen.\n\n"
        "RELATIVSATZ-BÜNDELUNGS-REGEL: Wenn ein Wirkstoff oder Objekt durch einen direkt integrierten "
        "Relativsatz ('der/die/das ...') charakterisiert wird, sind Hauptsatz + Relativsatz EIN Fakt. "
        "Identifikation und Eigenschaft/Wirkung nicht trennen.\n"
        "  Falsch: 2 Facts — claim='enthält den Wirkstoff Paracetamol' + claim='wirkt schmerzlindernd und fiebersenkend'\n"
        "  Richtig: 1 Fakt:\n"
        "    claim='wirkt schmerzlindernd und fiebersenkend'  [die Wirkung ist der claim, nicht die Wirkstoff-Identifikation]\n"
        "    source_span='DAFALGAN 500 mg enthält den Wirkstoff Paracetamol, der schmerzlindernd und fiebersenkend wirkt.'  [vollständiger Originalsatz]\n"
        "  Gilt analog für alle Sätze der Form 'enthält X, der/die/das Y bewirkt/ist/hat'.\n\n"
        "SOURCE_SPAN-REGEL: Exakter Originaltext. Kontextabhängige Folgesätze "
        "('daher', 'deshalb', 'dies', 'diese') mit ihrem Vorläufer-Satz zusammenfassen. "
        "Nicht trennen wenn Folgesätze ohne Vorläufer keinen Sinn ergeben.\n\n"
        "DOSIERUNGS-BÜNDELUNGS-REGEL: Einzeldosis, Maximaldosis und Einnahmeintervall "
        "für dieselbe Population gehören in EINEN Fakt.\n"
        "  Falsch: 3 separate Facts für '1-2 Tabletten', 'max. 8/Tag', 'alle 4-8 Stunden'\n"
        "  Richtig: 1 Fakt:\n"
        "    claim: '1–2 Tabletten bis 4× täglich (max. 8/Tag), Abstand mind. 4–8 Stunden'\n"
        "    population: 'Erwachsene und Jugendliche über 40 kg'\n"
        "    source_span: '1–2 Tabletten bis 4× täglich einnehmen (max. 8 Tabletten pro Tag). "
        "Einzeldosen nicht häufiger als alle 4–8 Stunden verabreichen.'\n"
        "  Die Dosierungszahlen + Intervall gehören in den claim — nur die Population wird herausgetrennt.\n\n"
        "SCHWANGERSCHAFT/STILLZEIT-POPULATIONS-REGEL: Wenn ein Fakt spezifisch für Schwangere oder Stillende gilt "
        "(egal ob Erlaubnis, Warnung oder Kontraindikation), MUSS population gesetzt werden:\n"
        "  Erwähnt claim oder source_span 'Schwangerschaft' → population='Schwangere'\n"
        "  Erwähnt claim oder source_span 'Stillzeit' oder 'Stillen' → population='Stillende'\n"
        "  Beides → population='Schwangere und Stillende'\n"
        "  Falsch: claim='können während der Schwangerschaft angewendet werden', population=null\n"
        "  Richtig: claim='können während der Schwangerschaft angewendet werden', population='Schwangere'\n"
        "  Dies gilt UNABHÄNGIG davon ob es eine Erlaubnis, Einschränkung oder ein Verbot ist.\n\n"
        "POPULATIONS-SPLIT-REGEL: Wenn für VERSCHIEDENE Populationen VERSCHIEDENE Regeln gelten "
        "→ PRO Population einen separaten Fakt (nicht in einem Fakt kombinieren):\n"
        "  Falsch: 1 Fakt mit 'Erwachsene: max. 5 Tage, Kinder bis 12: max. 3 Tage'\n"
        "  Richtig:\n"
        "    Fakt 1: claim='max. 5 Tage ohne ärztliche Verordnung', "
        "population='Erwachsene und Kinder ab 12 Jahren', bucket='dosierung'\n"
        "    Fakt 2: claim='max. 3 Tage ohne ärztliche Verordnung', "
        "population='Kinder bis 12 Jahren', bucket='dosierung'\n"
        "  Gilt für Dosierungsmengen, Behandlungsdauer und alle Anwendungsregeln.\n\n"
        "KONSOLIDIERUNGSREGEL: Semantisch zusammengehörende Konsequenzen und "
        "Handlungsanweisungen zu EINEM Claim zusammenfassen.\n"
        "  Falsch: 4 Facts 'Arzt rufen', 'auch ohne Symptome', 'Leberschaden möglich', "
        "'umgehend handeln' — alle mit condition='bei Überdosierung'\n"
        "  Richtig: 1 Fakt, claim='unverzüglich Arzt aufsuchen (auch ohne Symptome)', "
        "condition='bei Überdosierung', source_span=alle relevanten Sätze zusammen\n\n"
        "LISTEN-BÜNDELUNGS-REGEL: Wenn ein Abschnitt eine Liste von Arzneimitteln oder Bedingungen "
        "mit DERSELBEN übergeordneten Anweisung enthält → EINEN Fakt, vollständige Liste im source_span:\n"
        "  Wechselwirkungen:\n"
        "    Falsch: 7 separate Facts für Blutverdünner / Cholestyramin / Chloramphenicol / ...\n"
        "    Richtig: 1 Fakt, claim='Wechselwirkungen mit mehreren Arzneimitteln (Antikoagulanzien, "
        "Antiepileptika, Antibiotika u.a.)', source_span=vollständige Wechselwirkungsliste\n"
        "  Arzt-Konsultations-Listen:\n"
        "    Falsch: 3 separate Facts für 'schwere Infektion' / 'Untergewicht' / 'Alkoholkonsum'\n"
        "    Richtig: 1 Fakt, claim='Arzt konsultieren bei bestimmten Risikofaktoren', "
        "source_span=vollständige Liste aller Bedingungen\n"
        "  Prüftest: Haben alle Listenpunkte dieselbe übergeordnete Anweisung? → Bündeln.\n"
        "  UNTERSCHIED ZU ANTI-OMISSION: LISTEN-BÜNDELUNGS gilt wenn die Liste die BEDINGUNG ist "
        "(Wechselwirkungen: 'bei Substanz A, B, C' → bündeln). "
        "ANTI-OMISSION gilt wenn die Liste die KERNAUSSAGE ist "
        "(Indikationen: 'bei Kopfschmerzen, Zahnschmerzen, Fieber' → alle vollständig nennen).\n\n"
        "ANTI-OMISSION-REGEL: Wenn ein Satz mehrere gleichwertige Einträge auflistet, "
        "müssen ALLE vollständig im claim stehen. Kürze niemals Aufzählungen ab.\n"
        "  Falsch: claim='zur Behandlung von Kopfschmerzen'  [Zahnschmerzen, Fieber weggelassen]\n"
        "  Richtig: claim='zur Behandlung von Kopfschmerzen, Zahnschmerzen und Fieber'\n"
        "  Falsch: claim='erhöhtes Risiko bei Herz-, Leber- ...' [Satz abgebrochen]\n"
        "  Richtig: Alle genannten Organe/Zustände vollständig aufführen.\n\n"
        "BUCKET-REGEL: Jeder Fakt gehört in genau EINEN Bucket — extrahiere denselben Inhalt nie zweimal. "
        "nicht_nehmen = nur absolute Verbote (darf nicht einnehmen). "
        "WICHTIG: Positive Erlaubnisse ('kann angewendet werden', 'ist erlaubt', 'ist möglich') "
        "gehören NIEMALS in nicht_nehmen — immer in beachten oder wofuer.\n"
        "beachten = Vorsichtshinweise, Warnungen, Arzt informieren, Wechselwirkungen.\n"
        "  Falsch: 'ärztliche Beratung erforderlich bei Herzerkrankung' → nicht_nehmen\n"
        "  Richtig: 'ärztliche Beratung erforderlich bei Herzerkrankung' → beachten\n"
        "  Falsch: 'kann während der Schwangerschaft angewendet werden' → nicht_nehmen\n"
        "  Richtig: 'kann während der Schwangerschaft angewendet werden' → beachten\n"
        "  Falsch: 'mit Wasser einnehmen' gleichzeitig in dosierung UND beachten\n"
        "  Richtig: 'mit Wasser einnehmen' → nur dosierung\n"
        "  BEHANDLUNGSDAUER → IMMER dosierung, nicht beachten:\n"
        "    Falsch: 'darf nicht länger als 5 Tage angewendet werden' → beachten\n"
        "    Richtig: 'darf nicht länger als 5 Tage angewendet werden' → dosierung\n\n"
        "LESE-PRIORITÄT — ZUERST P4-CHECK, dann P1/P2/P3:\n\n"
        "SCHRITT 1 — P4-CHECK (für JEDEN Satz als erstes, vor allem anderen):\n"
        "  Frage: 'Ist dies eine juristische Absicherung, eine generische Floskel oder eine Binsenweisheit, "
        "die keinen spezifischen medizinischen Mehrwert für genau DIESES Medikament liefert?'\n"
        "  JA → P4. Sofort stoppen, nicht weiter prüfen.\n\n"
        "  P4-Kategorien (JA wenn eine davon zutrifft):\n"
        "  (a) Generische Compliance ohne Zahlenwert: 'Dosierung nicht überschreiten', 'nach Anweisung einnehmen', 'nicht von sich aus ändern'\n"
        "  (b) Arzt-Weiterleitung ohne spezifischen Auslöser — auch mit generischer Bedingung: "
        "'fragen Sie Ihren Arzt', 'wenden Sie sich an Ihren Apotheker', "
        "'falls Schmerzen nicht gelindert werden', 'bei Fortbestehen der Symptome Arzt konsultieren'\n"
        "  (c) Klassen-Boilerplate: Standardaussagen die für eine ganze Wirkstoffklasse gelten, "
        "nicht für dieses spezifische Medikament: "
        "'Schmerzmittel können bei Dauergebrauch Kopfschmerzen verursachen', "
        "'langdauernder Gebrauch von Schmerzmitteln kann Kopfschmerzen begünstigen'\n"
        "  (d) Reassurance/Entwarnung ohne Einschränkung: 'hat keinen Einfluss auf X', 'Risiko ist gering', 'als vereinbar betrachtet'\n"
        "  (e) Generische Zeitregel ohne konkrete Zahl: 'so kurz wie möglich', 'kürzest möglichen Zeitraum', 'geringstmögliche Dosis'\n"
        "  (f) Aufbewahrungs- und Handhabungshinweise: 'Für Kinder unzugänglich aufbewahren', 'bei Raumtemperatur lagern'\n\n"
        "  Entscheidungspaare:\n"
        "    'Dosierung nicht überschreiten' → generisch, kein Zahlenwert → P4\n"
        "    'max. 8 Tabletten täglich' → spezifische Zahl → NEIN → weiter zu Schritt 2\n"
        "    'Arzt fragen bei Fragen zur Anwendung' → generische Weiterleitung → P4\n"
        "    'Wenden Sie sich an Arzt falls Schmerzen nicht gelindert werden' → generische Weiterleitung mit generischer Bedingung → P4\n"
        "    'Bei Fieber oder Symptomen über mehr als 3 Tage Arzt konsultieren' → gilt für jedes OTC-Fiebermittel → P4\n"
        "    'Arzt fragen bei Glucose-6-Phosphat-Dehydrogenase-Mangel' → spezifischer seltener Auslöser → NEIN → P3\n"
        "    'Hat keinen Einfluss auf Fahrtüchtigkeit' → Entwarnung → P4\n"
        "    'Fahrtüchtigkeit kann beeinträchtigt sein' → konkrete Warnung → NEIN → P1\n"
        "    'Schmerzmittel sollen nicht langfristig eingenommen werden' → Klassen-Boilerplate → P4\n"
        "    'nicht länger als 5 Tage anwenden' → konkrete Tageszahl → NEIN → P1/P2\n"
        "    'Risiko für das Kind ist gering' → Reassurance → P4\n"
        "    'Paracetamol tritt in Muttermilch über' → wirkstoffspezifisch → NEIN → P2/P3\n"
        "    'enthält weniger als 1 mmol Natrium' → bürokratische Inhaltsstoff-Entwarnung → P4\n\n"
        "SCHRITT 2 — NUR für Fakten die P4-Check überlebt haben (NEIN in Schritt 1):\n"
        "KEIN medizinisches Gefahren-Rating. Seltene aber schwere Wechselwirkungen = P3, weil die Masse der User sie nie braucht.\n\n"
        "P1 — Sofort lesen:\n"
        "  Wofür: Hauptindikation (Schmerz, Entzündung, Fieber — nicht jede Einzelindikation)\n"
        "  Dosierung: Maximaldosis und Altersgrenze für JEDE Alters-/Gewichtsgruppe → jeweils P1:\n"
        "    Richtig P1: Erwachsene >40 kg Maximaldosis\n"
        "    Richtig P1: Kinder 30–40 kg Maximaldosis\n"
        "    Richtig P1: Kinder 22–30 kg Maximaldosis  ← nicht P2\n"
        "    Richtig P1: Kinder 15–22 kg Maximaldosis  ← nicht P2\n"
        "    (Vergib P1 für JEDE Altersgruppe — Python-Postprocessing kürzt danach falls nötig)\n"
        "  Nicht nehmen: häufigste absolute Kontraindikation (Allergie auf Wirkstoff, schwere Leber-/Niereninsuffizienz, Schwangerschaft letztes Trimenon)\n"
        "  Beachten: akute Alltagsgefahren (Fahrtüchtigkeit EINGESCHRÄNKT — nicht 'kein Einfluss'!, kein Alkohol)\n\n"
        "P2 — Sollte gelesen werden:\n"
        "  Weitere Indikationen (Zahnschmerzen, Kopfschmerzen, Menstruation — wenn nicht schon P1)\n"
        "  Einnahmeregeln mit konkretem Wert (mit Wasser, nach dem Essen, Abstand in Stunden)\n"
        "  Schwangerschaft/Stillzeit | bekannte Wechselwirkungen mit HÄUFIGEN Mitteln (Blutverdünner, andere NSAIDs, Alkohol)\n\n"
        "P3 — Nur für spezielle User:\n"
        "  Wechselwirkungen mit SPEZIALMITTELN (Mittel für chronische/seltene Erkrankungen):\n"
        "    TB: Rifampicin, Isoniazid | Epilepsie: Phenytoin, Phenobarbital, Carbamazepin\n"
        "    HIV: Zidovudin | Gicht: Probenecid | Blutfette: Cholestyramin | Infektion: Chloramphenicol\n"
        "    Faustregel: Spezialmittel = nur Patienten mit chronischer Erkrankung → P3\n"
        "  Wirkmechanismus | Lagerungshinweise\n"
        "  Sehr seltene Nebenwirkungen ('in sehr seltenen Fällen', 'äusserst selten')\n"
        "  Vorsicht bei seltenen Zuständen (Untergewicht, Glucose-6-Phosphat-Dehydrogenase-Mangel, Meulengracht)\n"
        "  EXPLIZIT P3 (nicht P2!) — diese Typen landen oft fälschlicherweise bei P2:\n"
        "    Elaborierungen: Erklärungen oder Hintergründe die einen P1/P2-Fakt ausführen\n"
        "      → 'Dies liegt daran, dass Ibuprofen die Prostaglandin-Synthese hemmt' = P3\n"
        "    Klinische Symptombeschreibungen: detaillierte Beschreibungen wie eine NW sich äussert\n"
        "      → 'kann sich als Hautrötung, Juckreiz, Nesselsucht, Schwellung oder Atemnot zeigen' = P3\n"
        "      (Der übergeordnete Fakt 'allergische Reaktionen möglich' ist P2 — die Symptomaufzählung P3)\n"
        "    Generische Wirkstoffklassen-Warnungen: Aussagen die für ALLE NSAIDs oder ALLE Analgetika gelten,\n"
        "      nicht spezifisch für dieses Medikament\n"
        "      → 'wie alle NSAR kann es Magen-Darm-Beschwerden verursachen' = P3\n\n"
        "WICHTIG: Das interne Gefahrenwissen des Modells darf die Lese-Priorität NICHT übersteuern.\n"
        "'Dosierung nicht überschreiten' klingt wichtig — ist aber P4 (Schritt 1), weil es keine\n"
        "medikamentenspezifische Information enthält. P4 = kein Informationswert für DIESES Medikament."
    )

    # PIL-Text nach Abschnittsmarkern [Überschrift] in Chunks aufteilen.
    # Jeder Chunk wird separat verarbeitet um das 4096-Token-Output-Limit zu vermeiden.
    import re
    sections = re.split(r'(?=\[)', relevanter_rohtext)
    sections = [s.strip() for s in sections if s.strip()]

    # Einzelne Sections die zu lang sind weiter nach Sätzen/Zeilen aufteilen
    CHUNK_MAX = 1500
    split_sections = []
    for sec in sections:
        if len(sec) <= CHUNK_MAX:
            split_sections.append(sec)
        else:
            # Header extrahieren (erste Zeile mit [...])
            header_match = re.match(r'(\[[^\]]+\])\s*', sec)
            header = header_match.group(0) if header_match else ""
            body = sec[len(header):]
            # Nach Sätzen oder Zeilenumbrüchen trennen
            sentences = re.split(r'(?<=[.!?])\s+|\n+', body)
            sub = header
            for sent in sentences:
                if not sent.strip():
                    continue
                if len(sub) + len(sent) > CHUNK_MAX and sub.strip() not in (header.strip(), ""):
                    split_sections.append(sub.strip())
                    sub = sent
                else:
                    sub += " " + sent if sub else sent
            if sub.strip():
                split_sections.append(sub.strip())

    # Split-Sections zu Chunks zusammenfassen
    chunks = []
    current = ""
    for sec in split_sections:
        if len(current) + len(sec) > CHUNK_MAX and current:
            chunks.append(current.strip())
            current = sec
        else:
            current += "\n\n" + sec if current else sec
    if current.strip():
        chunks.append(current.strip())

    all_facts = []
    for i, chunk in enumerate(chunks):
        user = (
            f"Medikament: {med_name}\n\n"
            f"{chunk}\n\n"
            "Extrahiere alle relevanten Fakten als atomic facts."
        )
        try:
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                system=system,
                tools=[EXTRACT_TOOL],
                tool_choice={"type": "tool", "name": "extract_pil_facts"},
                messages=[{"role": "user", "content": user}]
            )
            if resp.stop_reason == "max_tokens":
                print(f"[EXTRACT] Chunk {i+1}/{len(chunks)}: max_tokens erreicht — Chunk verkleinern", file=sys.stderr)
            if resp.content and hasattr(resp.content[0], 'input'):
                facts = resp.content[0].input.get("facts", [])
                all_facts.extend(facts)
                print(f"[EXTRACT] Chunk {i+1}/{len(chunks)}: {len(facts)} facts", file=sys.stderr)
        except Exception as e:
            import traceback
            print(f"[EXTRACT] Chunk {i+1} Fehler: {type(e).__name__}: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

    print(f"[EXTRACT] Gesamt: {len(all_facts)} facts aus {len(chunks)} Chunks (vor Dedup)", file=sys.stderr)
    seen_spans = set()
    deduped = []
    for f in all_facts:
        key = f.get('source_span', '').strip().lower()
        if key and key not in seen_spans:
            seen_spans.add(key)
            deduped.append(f)
        elif not key:
            deduped.append(f)
    if len(all_facts) - len(deduped):
        print(f"[EXTRACT] Dedupliziert: {len(all_facts) - len(deduped)} Duplikate entfernt", file=sys.stderr)
    all_facts = deduped
    calibrated = _calibrate_p1(all_facts)
    calibrated = _calibrate_p2(calibrated)
    calibrated = _force_p4_boilerplate(calibrated)
    p1_count = sum(1 for f in calibrated if f.get("lese_prioritaet") == 1)
    p4_count = sum(1 for f in calibrated if f.get("lese_prioritaet") == 4)
    print(f"[EXTRACT] Nach Kalibrierung: {p1_count} P1, {p4_count} P4 von {len(calibrated)} facts", file=sys.stderr)
    _extract_cache[cache_key] = calibrated
    return calibrated


def _calibrate_p1(facts: list) -> list:
    """Post-processing: Begrenzt P1 pro Bucket nach Modality-Priorität.
    dosierung bekommt ein höheres Limit (8) weil Altersgruppen-Dosierungen alle P1 bleiben müssen.
    Überschuss-P1 wird auf P2 abgestuft. P4 bleibt unberührt."""
    from collections import defaultdict

    # Modality-Rangfolge: je niedriger, desto eher bleibt es P1
    MODALITY_RANK = {
        "indication":         0,
        "contraindication":   1,
        "dosage_instruction": 2,
        "warning":            3,
        "drug_interaction":   4,
        "side_effect":        5,
        "dosage_adjustment":  6,
        "precaution":         7,
        "info":               8,
    }
    MAX_P1_DEFAULT  = 3
    MAX_P1_DOSIERUNG = 8  # Altersgruppen-Dosierungen: mehrere P1 sind legitim

    p1_by_bucket: dict = defaultdict(list)
    for i, f in enumerate(facts):
        if f.get("lese_prioritaet") == 1:
            p1_by_bucket[f.get("bucket", "beachten")].append(i)

    demote: set = set()
    for bucket, indices in p1_by_bucket.items():
        max_p1 = MAX_P1_DOSIERUNG if bucket == "dosierung" else MAX_P1_DEFAULT
        if len(indices) <= max_p1:
            continue
        ranked = sorted(indices, key=lambda i: MODALITY_RANK.get(facts[i].get("modality", "info"), 9))
        for i in ranked[max_p1:]:
            demote.add(i)

    if not demote:
        return facts

    result = []
    for i, f in enumerate(facts):
        result.append({**f, "lese_prioritaet": 2} if i in demote else f)
    return result


def _calibrate_p2(facts: list, bucket: str = "beachten", max_p2: int = 6) -> list:
    """Post-processing: Begrenzt P2 im angegebenen Bucket auf max_p2 nach Modality-Priorität.
    Überschuss wird auf P3 abgestuft — info/precaution zuerst, side_effect danach.
    Parallel zu _calibrate_p1 um beachten-Bucket-Bloat zu reduzieren."""
    DEMOTE_ORDER = [
        "info", "precaution", "side_effect", "drug_interaction", "dosage_adjustment",
        "warning", "dosage_instruction", "contraindication", "indication"
    ]

    p2_indices = [
        i for i, f in enumerate(facts)
        if f.get("lese_prioritaet") == 2 and f.get("bucket") == bucket
    ]

    if len(p2_indices) <= max_p2:
        return facts

    ranked = sorted(
        p2_indices,
        key=lambda i: DEMOTE_ORDER.index(facts[i].get("modality", "info"))
                      if facts[i].get("modality", "info") in DEMOTE_ORDER else 9,
        reverse=True  # höchster Index = niedrigste Priorität → zuerst demoten
    )
    demote = set(ranked[:len(p2_indices) - max_p2])

    return [{**f, "lese_prioritaet": 3} if i in demote else f for i, f in enumerate(facts)]


def _force_p4_boilerplate(facts: list) -> list:
    """Deterministisch: Strukturelle P4-Erkennung — medikament-unabhängig.

    Logik:
    1. Kein spezifischer Zahlenwert → nie P4-Kandidat (Dosierungsangaben mit Zahlen bleiben)
    2. Generisches Compliance-/Deferral-Muster OHNE spezifischen Inhalt → P4
    3. Kurzer Claim (<70 Zeichen) OHNE spezifischen medizinischen Inhalt → P4
    Schützt echte Warnings durch _SPECIFIC_CONTENT-Regex.
    """
    import re as _re

    _SPECIFIC_VALUE = _re.compile(
        r'\d+\s*(tablette|kapsel|mg|ml|tropfen|tage|stunden|wochen|monate|mal|\%|jahr\w*|jährig\w*)', _re.I
    )

    _SPECIFIC_CONTENT = _re.compile(
        r'paracetamol|ibuprofen|aspirin|codein|wirk\w+|'
        r'allergi\w+|überempfindlich\w+|'
        r'leber\w*|nieren?\w*|herzfunk\w*|'
        r'asthma|diabetes|blutdruck|'
        r'schwanger\w*|stillzeit|stillen|'
        r'alkohol|überdosi\w+|'
        r'hautreakt\w*|blutung\w*|infekt\w+|kreislauf|schwindel|übelkeit|erbrechen|'
        r'wechselwirk\w+|'
        r'zidovudin|rifampicin|phenytoin|phenobarbital|carbamazepin|'
        r'blutverdünn\w+|cholestyramin|chloramphenicol|metoclopramid|'
        r'probenecid|isoniazid|'
        r'glucose.6|g6pd|meulengracht|'
        r'leberschäd\w*|nierenschäd\w*|'
        r'kinder\w*|kind\b|säugling\w*|neugeboren\w*|jugendlich\w*|kleinkind\w*|'
        r'ältere?\s*patient\w*|ältere?\s*person\w*|geburtstermin|neugeboren\w*',
        _re.I
    )

    _GENERIC_PATTERN = _re.compile(
        r'(überschrit\w*|'                       # "überschritten werden" (Partizip)
        r'überschreit\w*|'                       # "überschreiten" (Infinitiv/Präsens)
        r'nicht von sich aus|'
        r'nach anweisung|wie verschrieben|wie verordnet|genau nach\b|exakt nach\b|'
        r'sprechen sie mit\b|fragen sie ihr\w* arzt|wenden sie sich an\b|'
        r'einfluss auf die verkehrstüchtigkeit|einfluss auf das bedienen|'
        r'bedürfen einer ärztlich\w*|'
        r'ohne ärztliche kontrolle|'
        r'kürzest möglichen zeitraum|geringstmögliche dosis|'
        r'längere zeit regelmässig)',
        _re.I
    )

    result = []
    for f in facts:
        if f.get("lese_prioritaet", 4) == 4:
            result.append(f)
            continue

        if f.get("modality") in ("contraindication", "indication"):
            result.append(f)
            continue

        claim = (f.get("claim")       or "").lower()
        span  = (f.get("source_span") or "").lower()

        # Spezifischen Wert oder spezifischen Inhalt in claim ODER source_span prüfen.
        # WICHTIG: claim ist nach CLAIM-REGEL minimiert — Kontext steckt im source_span.
        has_specific_value   = bool(_SPECIFIC_VALUE.search(claim)   or _SPECIFIC_VALUE.search(span))
        has_specific_content = bool(_SPECIFIC_CONTENT.search(claim) or _SPECIFIC_CONTENT.search(span))
        has_generic_pattern  = bool(_GENERIC_PATTERN.search(claim))

        if has_specific_value:
            result.append(f)
        elif has_generic_pattern and not has_specific_content:
            print(f"[P4-OVERRIDE] generic_pattern: '{f.get('claim')}' (prio war {f.get('lese_prioritaet')})", file=sys.stderr)
            result.append({**f, "lese_prioritaet": 4})
        elif not has_specific_content and len(claim) < 70:
            print(f"[P4-OVERRIDE] short+no-content: '{f.get('claim')}' (prio war {f.get('lese_prioritaet')})", file=sys.stderr)
            result.append({**f, "lese_prioritaet": 4})
        else:
            result.append(f)
    return result


# ── Schicht 3: TRANSFORM ─────────────────────────────────────────────────────

_TRANSFORM_SYSTEM = """Du bist ein medizinischer Informationsassistent für einen UX-Forschungsprototyp.

Du erhältst eine Liste von Fakten, jeweils mit claim (Kernaussage), source_span (Originaltext), bucket und optional population/condition.

Für jeden Fakt generierst du genau 5 Textvarianten:

WICHTIGE REGEL FÜR ALLE VARIANTEN:
Streiche IMMER Alters- und Bevölkerungsgruppen-Prefixe (z.B. "Erwachsene und Jugendliche über 40 kg:", "Für Kinder ab 12 Jahren:", "Patienten mit...") aus kurz_default, kurz_einfach, mittel_default und mittel_einfach.
Der User hat seine Gruppe bereits im Fragebogen ausgewählt — diese Prefixe sind redundant und störend.
Ausnahme: bucket='nicht_nehmen' — dort die Gruppe nennen, weil sie definiert, für WEN das Verbot gilt.

AUFZÄHLUNGEN — VOLLSTÄNDIGE WÖRTER:
Schreibe Aufzählungen immer mit ausgeschriebenen Wörtern. Keine abgekürzten Komposita.
FALSCH: "Kopf-, Zahn-, Gelenkschmerzen"
RICHTIG: "Kopfschmerzen, Zahnschmerzen, Gelenkschmerzen"

kurz_default:
  Knapper Bullet-Point, max. 12 Wörter. Schriftsprache, direkt und prägnant.
  Schreibe NUR den Kern — kein Satzanfang mit "Es", "Das Mittel", "Der Patient".
  KEIN population- oder condition-Prefix (siehe oben).
  Wenn bucket='nicht_nehmen': Den spezifischen Ausschlussgrund aus dem source_span extrahieren, OHNE "darf nicht eingenommen werden" — der Bucket-Titel sagt das bereits.
    Beispiel: claim="darf nicht eingenommen werden", source_span="Bei Überempfindlichkeit auf Paracetamol..."
    → kurz_default: "Bei Paracetamol-Allergie oder Überempfindlichkeit."
    → FALSCH: "darf nicht eingenommen werden"

kurz_einfach:
  Nur der Kern, max. 10 Wörter, A2-Niveau. Keine Fachbegriffe, Alltagssprache.
  KEIN population- oder condition-Prefix (siehe oben).
  Wenn bucket='nicht_nehmen': Den Ausschlussgrund in einfacher Sprache, OHNE "darf nicht eingenommen werden".
    Beispiel: "Bei Allergie auf Paracetamol."

mittel_default:
  1–2 Sätze in natürlichem, klarem Deutsch. SCHREIBE FRISCH — kopiere den source_span NICHT.
  Enthalte: alle medizinisch relevanten Details (Zahlen, Dosierungen, Zeitangaben, Bedingungen) aus claim + source_span.
  Entferne aktiv: Markennamen, generische Einleitungen ("Es wird angewendet zur...", "Wie alle Fieber- und Schmerzmittel..."),
    Füllformeln ("ohne Verordnung des Arztes oder der Ärztin"), redundante Wiederholungen, Bevölkerungsgruppen-Prefixe (ausser bei nicht_nehmen).
  Das Ergebnis muss KÜRZER und DIREKTER sein als der source_span.
  Wenn bucket='nicht_nehmen': Den Ausschlussgrund direkt nennen ohne "darf nicht eingenommen werden".

mittel_einfach:
  Gleicher Inhalt wie mittel_default, aber A2-Niveau.
  Kurze Sätze, keine Fachbegriffe, Alltagssprache. Ebenfalls frisch geschrieben, nicht source_span kopiert.
  KEIN population- oder condition-Prefix (ausser bei nicht_nehmen).

lang_einfach:
  Den source_span vollständig in einfache Sprache übersetzen, A2-Niveau.
  Alle Inhalte erhalten, nichts weglassen.

MEDIZINISCHER INHALTSVERTRAG: Erfinde keine Informationen. Nur Inhalte aus claim und source_span.
Sprache: Deutsch"""

_TRANSFORM_CHUNK_SIZE = 10  # Facts pro API-Call


def _transform_fact_chunk(facts_chunk: list, global_offset: int, med_name: str) -> list:
    """LLM-Call: generiert 5 Textvarianten pro Fakt.
    lang.default ist deterministisch (= source_span) — kein LLM nötig."""
    if not facts_chunk:
        return []

    tool_name = "transform_facts"
    tool = {
        "name": tool_name,
        "description": "Generiert 5 Textvarianten pro Fakt.",
        "input_schema": {
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "fact_index":    {"type": "integer"},
                            "kurz_default":  {"type": "string"},
                            "kurz_einfach":  {"type": "string"},
                            "mittel_default":{"type": "string"},
                            "mittel_einfach":{"type": "string"},
                            "lang_einfach":  {"type": "string"},
                        },
                        "required": [
                            "fact_index", "kurz_default", "kurz_einfach",
                            "mittel_default", "mittel_einfach",
                            "lang_einfach"
                        ]
                    }
                }
            },
            "required": ["results"]
        }
    }

    lines = []
    for i, fact in enumerate(facts_chunk):
        idx = global_offset + i
        parts = [
            f"[{idx}]",
            f"bucket: {fact.get('bucket', '')}",
            f"claim: {fact.get('claim', '')}",
        ]
        if fact.get("population"):
            parts.append(f"population: {fact['population']}")
        if fact.get("condition"):
            parts.append(f"condition: {fact['condition']}")
        parts.append(f"source_span: {fact.get('source_span', '')}")
        lines.append("\n".join(parts))

    user = f"Medikament: {med_name}\n\nFAKTEN:\n" + "\n\n".join(lines)

    for attempt in range(3):
        try:
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=6144,
                system=_TRANSFORM_SYSTEM,
                tools=[tool],
                tool_choice={"type": "tool", "name": tool_name},
                messages=[{"role": "user", "content": user}]
            )
            if resp.stop_reason == "max_tokens":
                print(f"[TRANSFORM] max_tokens — Chunk verkleinern", file=sys.stderr)
            return resp.content[0].input.get("results", [])
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                wait = 20 * (attempt + 1)
                print(f"[TRANSFORM] 429 rate-limit, warte {wait}s (attempt {attempt+1})", file=sys.stderr)
                import time as _time
                _time.sleep(wait)
            else:
                raise
    return []




def _transform_pil_facts(atomic_facts: list, antworten: dict, med_name: str) -> dict:
    """Schicht 3: Transform — gibt {bucket: [atoms]} zurück.

    Ablauf:
      1. Filter: P4 entfernen + relevant_for_user deterministisch bestimmen
      2. Deterministisch: lang.default = source_span
      3. LLM: 5 Textvarianten pro Fakt (kurz.default, kurz.einfach, mittel.default, mittel.einfach, lang.einfach)
      4. Atom zusammenbauen
    """
    cache_key = f"v22_transform_{med_name}|{json.dumps(antworten, sort_keys=True)}"
    if cache_key in _transform_cache:
        return _transform_cache[cache_key]

    disk = _transform_cache_load(cache_key)
    if disk is not None:
        _transform_cache[cache_key] = disk
        return disk

    if not atomic_facts:
        return {"wofuer": [], "nicht_nehmen": [], "dosierung": [], "beachten": []}

    # SCHRITT 1: Filter — P4 + Relevanz (deterministisch, kein LLM)
    to_transform = [
        (orig_idx, f) for orig_idx, f in enumerate(atomic_facts)
        if f.get("lese_prioritaet", 4) < 4 and _fact_relevant_for_user(f, antworten)
    ]

    print(f"[TRANSFORM] {len(to_transform)}/{len(atomic_facts)} Fakten → LLM "
          f"(P1/P2/P3 + relevant für User)", file=sys.stderr)

    # SCHRITT 3: LLM für 4 Felder pro Fakt
    chunk_input = [f for _, f in to_transform]
    chunks = [
        chunk_input[i:i + _TRANSFORM_CHUNK_SIZE]
        for i in range(0, len(chunk_input), _TRANSFORM_CHUNK_SIZE)
    ]

    llm_results: dict[int, dict] = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_map = {
                executor.submit(_transform_fact_chunk, chunk, i * _TRANSFORM_CHUNK_SIZE, med_name): i
                for i, chunk in enumerate(chunks)
            }
            for future in concurrent.futures.as_completed(future_map):
                chunk_idx = future_map[future]
                try:
                    for r in future.result():
                        fact_idx = r.get("fact_index")
                        if fact_idx is not None and 0 <= fact_idx < len(chunk_input):
                            llm_results[fact_idx] = r
                except Exception as e:
                    print(f"[TRANSFORM] Chunk {chunk_idx} Fehler: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[TRANSFORM] Executor Fehler: {e}", file=sys.stderr)

    # SCHRITT 4: Atoms zusammenbauen
    buckets: dict = {"wofuer": [], "nicht_nehmen": [], "dosierung": [], "beachten": []}
    for transform_idx, (orig_idx, fact) in enumerate(to_transform):
        llm = llm_results.get(transform_idx, {})
        source = fact.get("source_span", "")
        claim  = fact.get("claim", "")

        atom = {
            "bucket":            fact.get("bucket"),
            "lese_prioritaet":   fact.get("lese_prioritaet"),
            "claim":             claim,
            "population":        fact.get("population"),
            "condition":         fact.get("condition"),
            "modality":          fact.get("modality"),
            "negation":          fact.get("negation"),
            "source_span":       source,
            "relevant_for_user": True,
            "kurz": {
                "default": llm.get("kurz_default", claim),
                "einfach": llm.get("kurz_einfach",  claim),
            },
            "mittel": {
                "default": llm.get("mittel_default", source),
                "einfach": llm.get("mittel_einfach",  claim),
            },
            "lang": {
                "default": source,
                "einfach": llm.get("lang_einfach",  source),
            },
        }
        bucket = fact.get("bucket", "beachten")
        if bucket in buckets:
            buckets[bucket].append(atom)

    # Atoms innerhalb jedes Buckets sortieren: Priorität aufsteigend, dann nach Modality
    _MODALITY_SORT = {
        "indication": 0, "contraindication": 1, "dosage_instruction": 2,
        "warning": 3, "drug_interaction": 4, "side_effect": 5,
        "dosage_adjustment": 6, "precaution": 7, "info": 8,
    }
    _QUANTITY_WORDS = {"tablette", "tabletten", "kapsel", "kapseln", "mg", "ml", "tropfen", "×", "x"}
    _DURATION_WORDS = {"tage", "wochen", "monate", "jahre"}

    def _dosierung_type(atom: dict) -> int:
        """0 = Dosierungsmenge (Tabletten/mg), 1 = Dauer-Limit (Tage), 2 = Sonstiges."""
        claim = atom.get("claim", "").lower()
        has_qty  = any(w in claim for w in _QUANTITY_WORDS)
        has_dur  = any(w in claim for w in _DURATION_WORDS)
        if has_qty:
            return 0
        if has_dur and not has_qty:
            return 1
        return 2

    for key in buckets:
        if key == "dosierung":
            buckets[key].sort(key=lambda a: (
                _dosierung_type(a),
                a.get("lese_prioritaet", 4),
                _MODALITY_SORT.get(a.get("modality", "info"), 9)
            ))
        else:
            buckets[key].sort(key=lambda a: (
                a.get("lese_prioritaet", 4),
                _MODALITY_SORT.get(a.get("modality", "info"), 9)
            ))

    total = sum(len(b) for b in buckets.values())
    print(f"[TRANSFORM] Fertig: {total} atoms in {len(buckets)} Buckets", file=sys.stderr)

    _transform_cache[cache_key] = buckets
    _transform_cache_save(cache_key, buckets)
    return buckets


# ── Hilfsfunktion: kurzes Alters-Label aus Monaten ─────────────────────────
def _alters_label(min_monate: int, max_monate) -> str:
    """Generiert ein kurzes, einheitliches Alters-Label. Beispiele:
    (0, 36)   → 'unter 3 Jahren'
    (36, 72)  → '3 bis 6 Jahren'
    (144, None)→ 'ab 12 Jahren'
    """
    min_j = (min_monate or 0) // 12
    if max_monate is None:
        return f"ab {min_j} Jahren"
    max_j = max_monate // 12
    if min_monate == 0:
        return f"unter {max_j} Jahren"
    return f"{min_j} bis {max_j} Jahren"


# ── Onboarding-Hilfsfunktion: Fragen aus atomic_facts ableiten ─────────────
_SCHW_KW     = {"schwanger", "schwangerschaft"}
_STILL_KW    = {"still", "stillen", "stillzeit", "stillende"}
_SCHW_OPT    = ["Ja, ich bin schwanger", "Ja, ich stille", "Nein"]


def _parse_age_range(population: str) -> tuple[int | None, int | None]:
    """Extrahiert (min_jahre, max_jahre) aus einem population-String via Regex.
    Gibt (None, None) zurück wenn keine Jahreszahlen gefunden."""
    if not population:
        return None, None
    pop = population.lower()

    # "9–12 Jahre" / "9-12 Jahre"
    m = re.search(r'(\d+)\s*[-–]\s*(\d+)\s*jahr', pop)
    if m:
        return int(m.group(1)), int(m.group(2))

    # "ab 18 Jahren" / "über 12 Jahren" / "ab 65 Jahren"
    m = re.search(r'(?:ab|über|mindestens)\s*(\d+)\s*jahr', pop)
    if m:
        return int(m.group(1)), None

    # "unter 12 Jahren"
    m = re.search(r'unter\s*(\d+)\s*jahr', pop)
    if m:
        return None, int(m.group(1))

    # "bis 12 Jahren" — alternative Formulierung für obere Altersgrenze
    m = re.search(r'bis\s+(\d+)\s*jahr', pop)
    if m:
        return None, int(m.group(1))

    # "12 Jahre und älter" / "12 Jahre oder älter"
    m = re.search(r'(\d+)\s*jahre?\s*(?:und|oder)\s*älter', pop)
    if m:
        return int(m.group(1)), None

    # Kanonische Begriffe ohne explizite Jahreszahl im PIL
    if re.search(r'\berwachsen', pop):   # "Erwachsene", "Erwachsener"
        return 18, None
    if re.search(r'\bjugendlich', pop) and not re.search(r'\d', pop):  # "Jugendliche" ohne Zahl
        return 12, 17
    if re.search(r'\b(säugling|neugeboren)', pop):
        return 0, 1

    return None, None


def _age_label(f_min: int | None, f_max: int | None) -> str:
    """Formatiert ein Alters-Range-Paar als lesbares Label das von _parse_age_range() re-parsebar ist."""
    if f_min is not None and f_max is not None:
        return f"{f_min}–{f_max} Jahre"
    if f_min is not None:
        return f"ab {f_min} Jahren"
    if f_max is not None:
        return f"unter {f_max} Jahren"
    return ""


def _derive_alter_optionen(atomic_facts: list) -> list[str]:
    """Leitet Alters-Optionen dynamisch aus PIL-Facts ab.

    Schritt 1: Basis-Ranges aus dosierung-Bucket (positive Unter- und Obergrenzen).
    Schritt 2: Split-Grenzen aus nicht_nehmen-Bucket ("unter X Jahren") sammeln.
    Schritt 3: Basis-Ranges an intern liegenden Grenzen aufteilen (z.B. 6–17 → 6–11 + 12–17).
    """
    # ── Schritt 1: Basis-Ranges ──────────────────────────────────────────────
    base: dict[tuple, None] = {}  # (min, max) → None (nur als geordnetes Set)
    for f in atomic_facts:
        if f.get("lese_prioritaet", 4) == 4 or f.get("bucket") != "dosierung":
            continue
        pop = f.get("population") or ""
        f_min, f_max = _parse_age_range(pop)
        if f_min is None:
            continue  # "unter X" ohne Untergrenze = Ausschluss, keine wählbare Option
        # "Kinder/Jugendliche ab X" ohne Obergrenze → bis 17 kappen
        if f_max is None:
            pop_l = pop.lower()
            if re.search(r'\b(kinder|kind|jugendlich)', pop_l) and not re.search(r'\berwachsen', pop_l):
                f_max = 17
        base[(f_min, f_max if f_max is not None else 999)] = None

    if not base:
        return []

    # ── Schritt 2: Split-Grenzen aus nicht_nehmen sammeln ────────────────────
    splits: set[int] = set()
    for f in atomic_facts:
        if f.get("lese_prioritaet", 4) == 4:
            continue
        if f.get("bucket") not in ("nicht_nehmen", "beachten"):
            continue
        f_min, f_max = _parse_age_range(f.get("population") or "")
        if f_min is None and f_max is not None:
            splits.add(f_max)  # "unter X Jahren" → X ist potenzielle Teilungsgrenze

    # ── Schritt 3: Basis-Ranges an internen Grenzen aufteilen ────────────────
    final: dict[tuple, str] = {}
    for (d_min, d_max) in sorted(base):
        internal = sorted(s for s in splits if d_min < s < d_max)
        if not internal:
            hi = d_max if d_max != 999 else None
            final[(d_min, d_max)] = _age_label(d_min, hi)
        else:
            boundaries = [d_min] + internal + [d_max]
            for i in range(len(boundaries) - 1):
                lo = boundaries[i]
                # Untere Teil-Range endet bei split-1, letzter Teil endet bei d_max
                hi_raw = boundaries[i + 1]
                hi = (hi_raw - 1) if i < len(boundaries) - 2 else hi_raw
                hi_label = hi if hi != 999 else None
                final[(lo, hi)] = _age_label(lo, hi_label)

    return [label for _, label in sorted(final.items())]


def _onboarding_fragen_aus_facts(atomic_facts: list) -> list[dict]:
    alter_optionen = _derive_alter_optionen(atomic_facts)
    has_schw = False
    for f in atomic_facts:
        if f.get("lese_prioritaet", 4) == 4:
            continue
        # source_span einschliessen: population kann null sein obwohl Schwangerschaft erwähnt wird
        text = " ".join(filter(None, [
            f.get("population"), f.get("condition"), f.get("source_span")
        ])).lower()
        if any(kw in text for kw in _SCHW_KW) or any(kw in text for kw in _STILL_KW):
            has_schw = True
            break

    fragen = []
    if alter_optionen:
        fragen.append({"id": "altersgruppe",    "label": "Wie alt ist die Person?",              "type": "seg", "optionen": alter_optionen})
    if has_schw:
        fragen.append({"id": "schwangerschaft", "label": "Sind Sie schwanger oder stillen Sie?", "type": "seg", "optionen": _SCHW_OPT})
    return fragen


# ── API: Onboarding-Fragen ──────────────────────────────────────────────────
@app.route("/api/onboarding", methods=["POST"])
def api_onboarding():
    data         = request.get_json()
    atomic_facts = data.get("atomic_facts") or []
    med_name     = data.get("medikament_name", "")

    cache_key = f"v10_onboarding_{med_name}"
    if cache_key in _onboarding_cache:
        return jsonify({"success": True, "questions": _onboarding_cache[cache_key]})

    fragen = _onboarding_fragen_aus_facts(atomic_facts)
    _onboarding_cache[cache_key] = fragen
    return jsonify({"success": True, "questions": fragen})


def _build_prompt_v2(tile_content: dict, textlaenge: int, sprache: int, antworten: dict, med_name: str):
    """Baut System- und User-Prompt aus vorklassifizierten Tile-Buckets (keine rohen PIL-Felder)."""
    if textlaenge == 0:
        laenge_anweisung = "TEXTLÄNGE: Vollständig — alle relevanten Informationen übernehmen, nichts kürzen."
    elif textlaenge == 2:
        laenge_anweisung = "TEXTLÄNGE: Kompakt — auf das Wesentliche reduzieren, ca. halb so lang."
    else:
        laenge_anweisung = "TEXTLÄNGE: Sehr kurz — nur das Allerwichtigste, maximal 1-2 Punkte pro Tile."

    if sprache == 0 and textlaenge == 2:
        laenge_anweisung = "TEXTLÄNGE: Wähle die wichtigsten Sätze 1:1 aus dem Original — lass weniger relevante Passagen weg, aber formuliere nichts um."
    elif sprache == 0 and textlaenge == 4:
        laenge_anweisung = "TEXTLÄNGE: Nur die 1-2 wichtigsten Originalsätze pro Abschnitt, 1:1, ohne Umformulierung."

    if sprache == 0:
        sprach_anweisung = (
            "SPRACHE: Wähle relevante Sätze 1:1 aus dem Originaltext — kein Umformulieren, "
            "keine Synonyme. Lass nur weg: Querverweise auf andere Abschnitte "
            "('siehe Abschnitt X'), Zulassungsnummern, Natriumgehalt-Boilerplate, "
            "Abschnitte zu Lagerung/Haltbarkeit/Verpackung/Hersteller/Zulassungsinhaberin."
        )
    elif sprache == 2:
        sprach_anweisung = "SPRACHE: Standardsprache — klares Deutsch, Fachbegriffe erklären wo nötig."
    else:
        sprach_anweisung = "SPRACHE: Einfache Sprache (A2) — kurze Sätze, keine Fachbegriffe, Alltagssprache."

    kontext_zeilen = []
    for k, v in antworten.items():
        if k == "altersgruppe":
            kontext_zeilen.append(
                f"- Nutzer gehört zur Gruppe: \"{v}\"\n"
                f"  → In der Dosierung: NUR die Angaben für diese Gruppe zeigen, alle anderen weglassen.\n"
                f"  → Wenn diese Gruppe nicht im PIL erwähnt wird: Dosierungsabschnitt leer lassen und hinweisen."
            )
        elif k == "schwangerschaft":
            if "schwanger" in v.lower():
                kontext_zeilen.append("- Nutzerin ist schwanger → Schwangerschaftshinweis in 'beachten' prominent platzieren")
            elif "stille" in v.lower():
                kontext_zeilen.append("- Nutzerin stillt → Stillzeit-Hinweis in 'beachten' prominent platzieren")
    kontext_str = "\n".join(kontext_zeilen) if kontext_zeilen else "- Keine besonderen Kontext-Filter"

    def _item_text(item):
        """Extrahiert Text aus Item — unterstützt altes Format (str) und neues ({text, kritikalitaet})."""
        return item["text"] if isinstance(item, dict) else item

    wofuer_zeilen        = "\n".join(f"- {_item_text(s)}" for s in tile_content.get("wofuer_roh", []))
    nicht_nehmen_zeilen  = "\n".join(f"- {_item_text(s)}" for s in tile_content.get("nicht_nehmen_roh", []))
    dosierung_zeilen     = "\n".join(f"- {_item_text(s)}" for s in tile_content.get("dosierung_roh", []))
    beachten_zeilen      = "\n".join(f"- {_item_text(s)}" for s in tile_content.get("beachten_roh", []))

    vereinf_anweisung = (
        'Befülle "vereinfachungen" mit allen Fachbegriffen die du durch Alltagssprache ersetzt hast.'
        if sprache >= 4 else '"vereinfachungen" ist immer [].'
    )

    system = """Du bist ein medizinischer Informationsassistent für einen UX-Forschungsprototyp.

MEDIZINISCHER INHALTSVERTRAG (unverhandelbar):
- Erfinde keine Informationen. Nur Inhalte aus dem bereitgestellten Originaltext.
- Lass keine medizinisch relevante Information weg.
- Sprache: Deutsch
- Hinweise zu vergessener Dosis oder Überdosis gehören als beachten-Punkt mit typ "warnung" — NICHT in dosierung.
- Fehlen diese Hinweise im Originaltext, erfinde sie nicht."""

    user = f"""Medikament: {med_name}
{laenge_anweisung}
{sprach_anweisung}

NUTZER-KONTEXT:
{kontext_str}

PARADIGMA: Text
- punkte in wofuer: 2-4 klare Stichpunkte aus Patientenperspektive
- punkte in nicht_nehmen: Bedingungsformat "Wenn du X hast, dann nicht nehmen."
- dosierung.schritte: nummerierbare Handlungsschritte
- beachten.punkte: handlungsorientiert mit typ "warnung" oder "info"
- narrativ_panels: []

{vereinf_anweisung}

BEREITS KLASSIFIZIERTER INHALT (nur diese Informationen verwenden):

WOFÜR WIRD ES ANGEWENDET:
{wofuer_zeilen or "(keine Informationen vorhanden)"}

NICHT NEHMEN BEI:
{nicht_nehmen_zeilen or "(keine Informationen vorhanden)"}

DOSIERUNG & EINNAHME:
{dosierung_zeilen or "(keine Informationen vorhanden)"}

ZU BEACHTEN:
{beachten_zeilen or "(keine Informationen vorhanden)"}

Transformiere diesen Inhalt jetzt gemäss den obigen Anweisungen."""

    return system, user


def _fact_relevant_for_user(fact: dict, antworten: dict) -> bool:
    """True wenn der Fakt für diesen User gilt."""
    pop = (fact.get("population") or "").lower()
    if not pop:
        return True  # kein population = gilt für alle

    altersgruppe  = antworten.get("altersgruppe", "")
    schw_antwort  = antworten.get("schwangerschaft", "Nein").lower()
    ist_schwanger = "schwanger" in schw_antwort
    stillt        = "stille" in schw_antwort

    # Schwangerschaft / Stillzeit — kombinierte Strings ("Schwangere und Stillende") korrekt behandeln
    hat_schwanger_kw = any(kw in pop for kw in ("schwanger", "schwangerschaft"))
    hat_still_kw     = any(kw in pop for kw in ("still", "stillen", "stillzeit", "stillende"))
    if hat_schwanger_kw or hat_still_kw:
        passt_schwanger = hat_schwanger_kw and ist_schwanger
        passt_still     = hat_still_kw     and stillt
        return passt_schwanger or passt_still

    # Altersfilter: beide Seiten parsen und numerisch vergleichen
    f_min, f_max = _parse_age_range(fact.get("population") or "")
    u_min, u_max = _parse_age_range(altersgruppe)
    if (f_min is not None or f_max is not None) and (u_min is not None or u_max is not None):
        fact_min = f_min if f_min is not None else 0
        fact_max = f_max if f_max is not None else 999
        user_min = u_min if u_min is not None else 0
        user_max = u_max if u_max is not None else 999
        if fact_min > user_max or fact_max <= user_min:
            return False
        return True

    # Fallback: Keyword-Matching für Facts/Antworten ohne parsbare Jahreszahlen
    is_child_fact = any(kw in pop for kw in ("kinder", "kind", "säugling", "neugeboren", "jugendlich"))
    is_adult_user = altersgruppe in ("18 bis 64 Jahre", "65 Jahre oder älter")
    if is_child_fact and is_adult_user:
        return False

    return True




# ── Narrativ-Pipeline: Comic-Script + Comic-Bild ───────────────────────────



def _build_facts_text(atoms_by_bucket: dict) -> str:
    """Extrahiert P1+P2-Fakten (kurz.einfach) aus den atoms — Input für Comic-Generierung."""
    BUCKET_LABELS = {
        "wofuer":       "Wofür",
        "nicht_nehmen": "Nicht nehmen wenn",
        "dosierung":    "Dosierung & Einnahme",
        "beachten":     "Was du beachten solltest",
    }

    def facts_for_bucket(bucket_key):
        atoms = atoms_by_bucket.get(bucket_key, [])
        relevant = [a for a in atoms if a.get("relevant_for_user") and a.get("lese_prioritaet", 4) == 1]
        if not relevant:
            relevant = [a for a in atoms if a.get("relevant_for_user") and a.get("lese_prioritaet", 4) <= 2][:2]
        lines = []
        for a in relevant[:3]:
            text = (a.get("kurz", {}).get("einfach")
                    or a.get("kurz", {}).get("default")
                    or a.get("claim", ""))
            if text:
                lines.append(text)
        return "\n".join(lines)

    sections = []
    for i, key in enumerate(["wofuer", "nicht_nehmen", "dosierung", "beachten"], 1):
        text = facts_for_bucket(key)
        if text:
            sections.append(f"{i}. {BUCKET_LABELS[key]}\n{text}")
    return "\n\n".join(sections)


def _build_comic_prompt(facts: str, med_name: str) -> str:
    """Baut den Direkt-Prompt für gemini-3-pro-image (ein Modell, ein Call, kein Zwischenschritt)."""
    return f"""Create a single comic book page image: a 4-panel Lucky Luke comic strip (2×2 grid layout) that explains the package insert information for {med_name} in a fun, visual way.

TITLE BANNER at the very top of the page: "LUCKY LUKE ERKLÄRT: {med_name}" — bold, comic-book lettering.

PANEL 1 (top-left) — Title: "WOFÜR?":
Lucky Luke on Jolly Jumper, looking helpful. Show the medication's purpose visually through characters. Speech bubbles with very short German text (max 3–4 words each).

PANEL 2 (top-right) — Title: "NICHT NEHMEN WENN?":
Lucky Luke holding up a warning hand. Show contraindications using red STOP signs, crossed-out symbols, or worried characters. Speech bubbles with very short German text (max 3–4 words each).

PANEL 3 (bottom-left) — Title: "DOSIERUNG":
Lucky Luke carefully counting tablets. Show a calendar and a clock to indicate timing and duration limits. Speech bubbles with very short German text (max 3–4 words each).

PANEL 4 (bottom-right) — Title: "WAS BEACHTEN?":
Urgent scene — a character looking alarmed, a doctor's office in the background. Lucky Luke pointing toward the doctor. Speech bubbles with very short German text (max 3–4 words each).

MEDICATION INFORMATION (use this content for the speech bubbles and visual storytelling):
{facts}

VISUAL STYLE: Classic Franco-Belgian bande dessinée in the style of Morris's Lucky Luke. Sharp black ink lines, vibrant flat coloring, no gradients, clean white gutters between panels. All speech bubble text must be legible, in German, maximum 3–4 words per bubble. The overall image must look like a clean, scanned comic book page."""


def _build_neutral_comic_prompt(facts: str, med_name: str) -> str:
    """Prompt für neutralen 4-Panel-Comic — minimalistisch, wenig Farbe, kein Lucky Luke."""
    return f"""Create a single comic book page image: a 4-panel comic strip (2×2 grid layout) that explains the package insert information for {med_name} in a clear, visual way.

TITLE BANNER at the very top of the page: "COMIC-BEIPACKZETTEL: {med_name}" — bold, comic-book lettering.

PANEL 1 (top-left) — Title: "WOFÜR?":
A friendly cartoon doctor speaking to a patient. Show the medication's purpose visually through the characters. Speech bubbles with very short German text (max 3–4 words each).

PANEL 2 (top-right) — Title: "NICHT NEHMEN WENN?":
Same cartoon characters, doctor holding up a warning hand. Show contraindications using crossed-out symbols or worried characters. Speech bubbles with very short German text (max 3–4 words each).

PANEL 3 (bottom-left) — Title: "DOSIERUNG":
Character carefully counting tablets. Show a calendar and a clock to indicate timing and duration limits. Speech bubbles with very short German text (max 3–4 words each).

PANEL 4 (bottom-right) — Title: "WAS BEACHTEN?":
Character looking attentive, pointing toward reminder symbols or a checklist. Speech bubbles with very short German text (max 3–4 words each).

MEDICATION INFORMATION (use this content for the speech bubbles and visual storytelling):
{facts}

VISUAL STYLE: Black and white comic book style — strictly no color. Sharp black ink lines on white paper, grey shading only where needed. Neutral, generic comic art style with no references to any specific comic series or character. Clean white gutters between panels. Generic friendly cartoon figures — no named or trademarked characters. All speech bubble text must be legible, in German, maximum 3–4 words per bubble. The overall image must look like a clean, scanned black-and-white comic book page."""


def _generate_comic_image(atoms_by_bucket: dict, med_name: str, character: str = 'neutral') -> str:
    """Narrativ-Pipeline: Fakten direkt → Gemini 3 Pro Image (Nano Banana Pro) → Comic-Bild."""
    import base64
    import google.auth
    from google import genai
    from google.genai import types
    from config import GEMINI_GCP_PROJECT

    _, detected_project = google.auth.default()
    project_id = GEMINI_GCP_PROJECT or detected_project
    if not project_id:
        raise ValueError("Google Cloud Projekt-ID fehlt. Trage GOOGLE_CLOUD_PROJECT in .env ein.")
    client = genai.Client(vertexai=True, project=project_id, location="us-central1")

    facts = _build_facts_text(atoms_by_bucket)
    if character == 'lucky_luke':
        prompt = _build_comic_prompt(facts, med_name)
    else:
        prompt = _build_neutral_comic_prompt(facts, med_name)

    print("\n── DIREKT-PROMPT AN GEMINI 3 PRO IMAGE ──────────")
    print(prompt)
    print("────────────────────────────────────────────\n")

    response = client.models.generate_content(
        model="gemini-3-pro-image",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"],
            image_config=types.ImageConfig(
                aspect_ratio="3:2",
                output_mime_type="image/png",
            ),
        ),
    )

    for part in response.candidates[0].content.parts:
        if part.inline_data:
            return base64.b64encode(part.inline_data.data).decode("utf-8")

    raise ValueError("gemini-3-pro-image hat kein Bild zurückgegeben.")


# ── Infografik-Pipeline ────────────────────────────────────────────────────

_INFOGRAFIK_REF_CACHE: dict = {}

def _load_ref_image(url: str) -> bytes:
    """Lädt ein Referenzbild per URL — gecacht im Modulscope (einmal pro Server-Start)."""
    if url not in _INFOGRAFIK_REF_CACHE:
        import urllib.request
        with urllib.request.urlopen(url, timeout=15) as r:
            _INFOGRAFIK_REF_CACHE[url] = r.read()
    return _INFOGRAFIK_REF_CACHE[url]


def _build_infografik_prompt(atoms_by_bucket: dict) -> str:
    """Infografik-Prompt. Wofür: P1+P2, kurz.default. Rest: P1, kurz.einfach. Klammern entfernt."""
    import re

    LABELS = {
        "wofuer":       "Wofür",
        "nicht_nehmen": "Nicht nehmen wenn",
        "dosierung":    "Dosierung & Einnahme",
        "beachten":     "Was du beachten solltest",
    }

    def zeilen_liste(key):
        max_prio = 2 if key == "wofuer" else 1
        use_default = key == "wofuer"
        texts = []
        for a in atoms_by_bucket.get(key, []):
            if not (a.get("relevant_for_user") and a.get("lese_prioritaet", 4) <= max_prio):
                continue
            if use_default:
                text = (a.get("kurz", {}).get("default")
                        or a.get("kurz", {}).get("einfach")
                        or a.get("claim", ""))
            else:
                text = (a.get("kurz", {}).get("einfach")
                        or a.get("kurz", {}).get("default")
                        or a.get("claim", ""))
            text = re.sub(r'\s*\([^)]*\)', '', text).strip().rstrip('.')
            if text:
                texts.append(text)
            if len(texts) >= 4:
                break
        return texts

    sections = "\n\n".join(
        f'"{LABELS[key]}"\n' + "\n".join(zeilen_liste(key))
        for key in ["wofuer", "nicht_nehmen", "dosierung", "beachten"]
    )

    return (
        "Use Images 1, 2, 3, and 4 as style references — apply their illustration style, fine-line technique, and rendering to all visuals.\n\n"
        "Fully re-render the following facts in the illustration style of Images 1, 2, 3, and 4.\n\n"
        "Each icon or visual element must have a short text label. "
        "The section labels (\"Wofür\", \"Nicht nehmen wenn\", \"Dosierung & Einnahme\", \"Was du beachten solltest\") are text only — no icon for the section labels themselves. "
        "No overall title. No medication name. White background. Visualize information iconically where possible.\n\n"
        "Create a 2×2 grid with these four sections:\n\n"
        + sections
    )


def _generate_infografik_image(atoms_by_bucket: dict, med_name: str) -> str:
    """Infografik-Pipeline: atoms → 4 Referenzbilder + Prompt → Gemini API → PNG."""
    import base64
    import google.auth
    from pathlib import Path
    from google import genai
    from google.genai import types
    from config import GEMINI_GCP_PROJECT

    MODEL = "gemini-3-pro-image"
    REF_DIR = Path(__file__).parent / "icons" / "referenceImage"
    REF_FILES = ["image12.jpg", "Group 41.png", "Group 42.png", "Group 43.png"]

    _, detected_project = google.auth.default()
    project_id = GEMINI_GCP_PROJECT or detected_project
    if not project_id:
        raise ValueError("Google Cloud Projekt-ID fehlt. Trage GOOGLE_CLOUD_PROJECT in .env ein.")
    client = genai.Client(vertexai=True, project=project_id, location="us-central1")
    prompt = _build_infografik_prompt(atoms_by_bucket)

    print("\n══ INFOGRAFIK → GEMINI API ══════════════════════")
    print(f"[MODELL] {MODEL}")
    print(f"[BILDER] {', '.join(REF_FILES)}")
    print(f"[TEXT]\n{prompt}")
    print("═════════════════════════════════════════════════\n")

    def mime(f):
        return "image/jpeg" if f.endswith(".jpg") else "image/png"

    contents = [
        types.Part.from_bytes(data=(REF_DIR / f).read_bytes(), mime_type=mime(f))
        for f in REF_FILES
    ]
    contents.append(types.Part(text=prompt))

    response = client.models.generate_content(
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"],
            image_config=types.ImageConfig(
                aspect_ratio="1:1",
                image_size="2K",
            ),
        ),
    )

    for part in response.parts:
        if part.inline_data is not None:
            return base64.b64encode(part.inline_data.data).decode("utf-8")

    raise ValueError(f"{MODEL} hat kein Infografik-Bild zurückgegeben.")


@app.route('/api/debug/narrativ-prompt')
def debug_narrativ_prompt():
    """Zeigt den Imagen-Prompt für eine GTIN ohne Bild zu generieren."""
    cache_files = [
        f for f in os.listdir('cache/transform')
        if f.endswith('.json')
    ]
    # Lade neuesten Transform-Cache (für Debug reicht das)
    for fname in sorted(cache_files, key=lambda f: os.path.getmtime(os.path.join('cache/transform', f)), reverse=True):
        try:
            with open(os.path.join('cache/transform', fname), encoding='utf-8') as f:
                import json as _dbg
                d = _dbg.load(f)
            atoms_by_bucket = d.get('buckets', {})
            med_name = d.get('cache_key', '').split('|')[0].replace('v21_transform_', '').replace('v20_transform_', '') or 'Medikament'
            facts = _build_facts_text(atoms_by_bucket)
            prompt = _build_comic_prompt(facts, med_name)
            html = (
                f'<pre style="white-space:pre-wrap;font-family:monospace;padding:20px;'
                f'background:#f0f4ff;border-bottom:2px solid #aac">'
                f'── DIREKT-PROMPT AN GEMINI 3 PRO IMAGE ──────────\n\n{prompt}\n</pre>'
                f'<pre style="white-space:pre-wrap;font-family:monospace;padding:20px;color:#555;background:#f9f9f9">'
                f'── GEMINI 3 PRO IMAGE (Nano Banana Pro) → Comic-Bild ──\n'
                f'[Nur beim echten /api/narrativ-Call sichtbar — erscheint dann im Terminal]\n</pre>'
            )
            return html
        except Exception:
            continue
    return 'Kein Transform-Cache gefunden. Zuerst ein Medikament scannen.', 404


@app.route('/api/narrativ', methods=['POST'])
def api_narrativ():
    """Narrativ-Pipeline: atoms → Comic-Script (aus atoms aufgebaut) → Comic-Bild (Gemini). Gecacht."""
    import hashlib
    import json as _json

    import json as _json2
    raw = request.get_data()
    data = _json2.loads(raw.decode('utf-8'))
    atoms_by_bucket = data.get('atoms', {})
    med_name = data.get('medikament_name', 'Medikament')
    character = data.get('character', 'neutral')

    relevant_flat = [
        a for bucket in atoms_by_bucket.values()
        for a in bucket
        if a.get('relevant_for_user') and a.get('lese_prioritaet', 4) <= 2
    ]
    cache_key = hashlib.md5(
        _json.dumps(relevant_flat, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    cache_path = os.path.join('cache', 'narrativ_v2', f'{character}_{cache_key}.json')

    if os.path.exists(cache_path):
        with open(cache_path, encoding='utf-8') as f:
            return jsonify(_json.load(f))

    try:
        image_b64 = _generate_comic_image(atoms_by_bucket, med_name, character)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

    result = {'success': True, 'image_b64': image_b64}

    os.makedirs(os.path.join('cache', 'narrativ_v2'), exist_ok=True)
    with open(cache_path, 'w', encoding='utf-8') as f:
        _json.dump(result, f, ensure_ascii=False)

    return jsonify(result)


@app.route('/api/infografik', methods=['POST'])
def api_infografik():
    """Infografik-Pipeline: atoms → medizinische Infografik (Gemini). Gecacht."""
    import hashlib
    import json as _json

    data = request.get_json()
    atoms_by_bucket = data.get('atoms', {})
    med_name = data.get('medikament_name', 'Medikament')

    relevant_flat = [
        a for bucket in atoms_by_bucket.values()
        for a in bucket
        if a.get('relevant_for_user') and a.get('lese_prioritaet', 4) <= 2
    ]
    cache_key = hashlib.md5(
        _json.dumps(relevant_flat, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    cache_path = os.path.join('cache', 'infografik_v1', f'{cache_key}.json')

    if os.path.exists(cache_path):
        with open(cache_path, encoding='utf-8') as f:
            return jsonify(_json.load(f))

    try:
        image_b64 = _generate_infografik_image(atoms_by_bucket, med_name)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

    result = {'success': True, 'image_b64': image_b64}

    os.makedirs(os.path.join('cache', 'infografik_v1'), exist_ok=True)
    with open(cache_path, 'w', encoding='utf-8') as f:
        _json.dump(result, f, ensure_ascii=False)

    return jsonify(result)


# ── API: Atomize / Transform (Schicht 3) ───────────────────────────────────
@app.route("/api/atomize", methods=["POST"])
def api_atomize():
    """Schicht 3: Transformiert atomic_facts → enriched atoms mit Display-Texten und relevant_for_user."""
    data         = request.get_json()
    atomic_facts = data.get("atomic_facts")
    antworten    = data.get("onboarding_antworten", {})
    med_name     = data.get("medikament_name", "")

    if atomic_facts is None:
        return jsonify({"success": False, "error": "atomic_facts fehlen im Request"}), 400

    result = _transform_pil_facts(atomic_facts, antworten, med_name)
    return jsonify({"success": True, "atoms": result})


# ── API: Debug-Pipeline ─────────────────────────────────────────────────────
@app.route("/api/debug/pipeline/<gtin>", methods=["GET"])
def api_debug_pipeline(gtin):
    """Debug: Parse + Extract für eine GTIN. Speichert Ergebnis in debug/pipeline_<gtin>.json."""
    swissmedic_id = gtin_zu_swissmedic_id(gtin)
    if not swissmedic_id:
        return jsonify({"error": f"GTIN {gtin} nicht gefunden"}), 404

    eintrag = eintrag_fuer_barcode_string(gtin) or eintrag_dynamisch_laden(swissmedic_id)
    if not eintrag:
        return jsonify({"error": "Kein Beipackzettel-Eintrag gefunden"}), 404

    lokale_datei = eintrag.get("lokale_datei")
    if not lokale_datei:
        return jsonify({"error": "Beipackzettel-Datei nicht verfügbar"}), 404

    pil = parse_beipackzettel(lokale_datei)
    relevanter_rohtext = pil.get("relevanter_rohtext", "")
    med_name = _clean_med_name(eintrag.get("beschreibung", pil.get("medikament", "Unbekannt")))
    atomic_facts = _extract_pil_facts(relevanter_rohtext, med_name)

    debug_dir = Path(__file__).parent / "debug"
    debug_dir.mkdir(exist_ok=True)
    debug_data = {
        "medikament":         med_name,
        "relevanter_rohtext": relevanter_rohtext,
        "atomic_facts":       atomic_facts,
        "facts_count":        len(atomic_facts),
    }
    (debug_dir / f"pipeline_{gtin}.json").write_text(
        json.dumps(debug_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return jsonify(debug_data)



# ── API: Debug-Transform ─────────────────────────────────────────────────────
@app.route("/api/debug/transform/<gtin>", methods=["GET"])
def api_debug_transform(gtin):
    """Debug: Extract + Transform (Schicht 3) für eine GTIN.
    Query-Params: altersgruppe=ab+12+Jahren, schwangerschaft=Nein
    Speichert Ergebnis in debug/transform_<gtin>.json."""
    swissmedic_id = gtin_zu_swissmedic_id(gtin)
    if not swissmedic_id:
        return jsonify({"error": f"GTIN {gtin} nicht gefunden"}), 404

    eintrag = eintrag_fuer_barcode_string(gtin) or eintrag_dynamisch_laden(swissmedic_id)
    if not eintrag:
        return jsonify({"error": "Kein Beipackzettel-Eintrag gefunden"}), 404

    lokale_datei = eintrag.get("lokale_datei")
    if not lokale_datei:
        return jsonify({"error": "Beipackzettel-Datei nicht verfügbar"}), 404

    antworten = {}
    if request.args.get("altersgruppe"):
        antworten["altersgruppe"] = request.args.get("altersgruppe")
    if request.args.get("schwangerschaft"):
        antworten["schwangerschaft"] = request.args.get("schwangerschaft")

    pil = parse_beipackzettel(lokale_datei)
    relevanter_rohtext = pil.get("relevanter_rohtext", "")
    med_name = _clean_med_name(eintrag.get("beschreibung", pil.get("medikament", "Unbekannt")))

    atomic_facts = _extract_pil_facts(relevanter_rohtext, med_name)
    buckets = _transform_pil_facts(atomic_facts, antworten, med_name)

    stats = {}
    for b, atoms in buckets.items():
        p_counts = {}
        for a in atoms:
            p = a.get("lese_prioritaet", "?")
            p_counts[p] = p_counts.get(p, 0) + 1
        stats[b] = {"total": len(atoms), "prioritaeten": p_counts}

    p4_count = sum(1 for f in atomic_facts if f.get("lese_prioritaet") == 4)

    debug_data = {
        "medikament":         med_name,
        "antworten":          antworten,
        "atomic_facts_count": len(atomic_facts),
        "p4_count":           p4_count,
        "stats":              stats,
        "buckets":            buckets,
    }
    debug_dir = Path(__file__).parent / "debug"
    debug_dir.mkdir(exist_ok=True)
    (debug_dir / f"transform_{gtin}.json").write_text(
        json.dumps(debug_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return jsonify(debug_data)


# ── Run ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cert = Path(__file__).parent / "cert.pem"
    key  = Path(__file__).parent / "key.pem"

    ssl_ctx = (str(cert), str(key)) if cert.exists() and key.exists() else None
    protocol = "https" if ssl_ctx else "http"

    import socket
    ip = socket.gethostbyname(socket.gethostname())
    print(f"\n{'='*50}")
    print(f"  Beipackzettel-Prototyp v2")
    print(f"  {protocol}://{ip}:5001")
    print(f"  {protocol}://localhost:5001")
    if not ssl_ctx:
        print(f"\n  HTTPS fehlt — Kamera nur auf localhost!")
        print(f"  Setup: brew install mkcert && mkcert {ip} localhost")
    print(f"{'='*50}\n")

    app.run(
        host="0.0.0.0",
        port=5001,
        debug=True,
        ssl_context=ssl_ctx
    )
