"""
vision_scanner.py
-----------------
Erkennt ein Medikament anhand eines Verpackungsfotos via Claude Vision API.
Wird als Fallback eingesetzt wenn kein Barcode im Bild gefunden wurde.

Öffentliche Einstiegsfunktion: medikament_aus_foto_erkennen()
"""

import json
import base64

import anthropic

import config
from modules.xml_parser import medikament_suchen


def _mime_type_erkennen(bild_bytes: bytes) -> str:
    """Erkennt den MIME-Typ anhand der Magic Bytes."""
    if bild_bytes[:2] == b"\xff\xd8":
        return "image/jpeg"
    if bild_bytes[:4] == b"\x89PNG":
        return "image/png"
    if bild_bytes[:4] == b"GIF8":
        return "image/gif"
    if bild_bytes[:4] == b"RIFF" and bild_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _alle_beschreibungen_laden() -> list[str]:
    """Gibt alle bekannten Medikament-Beschreibungen aus mapping.json zurück."""
    if not config.MAPPING_FILE.exists():
        return []
    with open(config.MAPPING_FILE, encoding="utf-8") as f:
        mapping = json.load(f)
    beschreibungen = []
    for eintraege in mapping.values():
        if isinstance(eintraege, list):
            for e in eintraege:
                b = e.get("beschreibung", "")
                if b:
                    beschreibungen.append(b)
    return beschreibungen


def medikament_aus_foto_erkennen(bild_bytes: bytes) -> dict | None:
    """
    Sendet das Bild an die Claude Vision API und identifiziert das Medikament
    anhand der Verpackungsaufschrift.

    Gibt den passenden mapping.json-Eintrag zurück oder None wenn nicht erkannt.
    """
    if len(bild_bytes) > 5_000_000:
        return None

    beschreibungen = _alle_beschreibungen_laden()
    if not beschreibungen:
        return None

    mime_type = _mime_type_erkennen(bild_bytes)
    bild_b64 = base64.standard_b64encode(bild_bytes).decode("utf-8")

    system_prompt = (
        "Du bist ein Medikamenten-Identifikationssystem für einen Forschungsprototyp.\n"
        "Deine Aufgabe: Identifiziere das Medikament auf dem Foto anhand der Verpackungsaufschrift.\n\n"
        "REGELN:\n"
        "- Lies den Produktnamen direkt von der Verpackung\n"
        "- Wähle ausschliesslich aus der gegebenen Liste bekannter Medikamente\n"
        "- Antworte nur mit validem JSON – kein Markdown, kein erklärender Text\n\n"
        "OUTPUT-FORMAT (zwingend):\n"
        '{"erkannt": true, "beschreibung": "exakter Name aus der Liste"}\n'
        "oder falls nicht erkannt:\n"
        '{"erkannt": false, "beschreibung": null}'
    )

    liste_text = "\n".join(f"- {b}" for b in beschreibungen)
    user_text = (
        f"Welches Medikament ist auf diesem Foto abgebildet?\n\n"
        f"Bekannte Medikamente in diesem System (nur aus dieser Liste wählen):\n"
        f"{liste_text}\n\n"
        f"Gib das JSON-Objekt gemäss den Anweisungen zurück."
    )

    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        antwort = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=256,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": bild_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": user_text,
                        },
                    ],
                }
            ],
        )
    except Exception:
        return None

    rohtext = antwort.content[0].text.strip()

    try:
        daten = json.loads(rohtext)
    except json.JSONDecodeError:
        return None

    if not daten.get("erkannt") or not daten.get("beschreibung"):
        return None

    # Erkannte Beschreibung im mapping.json suchen
    treffer = medikament_suchen(daten["beschreibung"])
    if not treffer:
        return None

    # Exakten Treffer bevorzugen, sonst ersten Treffer nehmen
    for t in treffer:
        if t.get("beschreibung", "").lower() == daten["beschreibung"].lower():
            return t
    return treffer[0]
