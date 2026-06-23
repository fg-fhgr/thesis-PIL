"""
xml_parser.py
-------------
Liest lokale HTML-Beipackzettel (Swissmedicinfo-Format) und extrahiert
die für den Prototyp relevanten Abschnitte als Rohtext.

Architektur: Blacklist-Ansatz.
Alle Abschnitte werden mitgenommen, ausser jene die explizit auf der
Ignore-Liste stehen (Lagerung, Zulassung, Hersteller, Nebenwirkungen etc.).
Die semantische Klassifizierung in Tile-Buckets übernimmt Claude (Schicht 2).

Die Funktion `parse_beipackzettel` ist der zentrale Einstiegspunkt.
"""

import re
from pathlib import Path
from lxml import html as lxml_html

import config


# ---------------------------------------------------------------------------
# Blacklist: Abschnitte die IMMER ignoriert werden.
# Überschriften werden case-insensitiv auf Teilstring-Match geprüft.
# ---------------------------------------------------------------------------

ABSCHNITT_IGNORIEREN = [
    "was ist ferner",           # "Was ist ferner zu beachten?"
    "was ist in ",              # "Was ist in X enthalten?"
    "was sind in ",             # "Was sind in X enthalten?"
    "zulassungsnummer",
    "zulassungsinhaber",
    "wo erhalten sie",          # "Wo erhalten Sie X?"
    "welche packungen",         # "Welche Packungen sind erhältlich?"
    "diese packungsbeilage",    # "Diese Packungsbeilage wurde ... geprüft"
    "information für pat",      # "Information(en) für Patientinnen und Patienten" (Titelseite)
    "nebenwirkungen",           # Nebenwirkungen gehören nicht in die 4 Tiles
    "unerwünschte wirkungen",   # alternative Formulierung für Nebenwirkungen
]

# Keyword-Mapping für Backward-Compat (alte Felder für /api/generate Fallback)
_COMPAT_KEYWORDS = {
    "kurzfassung": [
        "wann werden sie angewendet",
        "wann wird ",
        "und wann wird",
        "wofür wird ",
        "was sind ",
    ],
    "warnhinweise": [
        "nicht eingenommen werden",
        "nicht angewendet werden",
        "nicht angewandt werden",
        "kontraindikation",
        "gegenanzeigen",
        "vorsicht geboten",
        "wechselwirkungen",
        "schwangerschaft",
        "vorsicht",
    ],
    "dosierung": [
        "wie verwenden sie",
        "wie nehmen sie",
        "wie wird ",
        "dosierung",
        "anwendung",
    ],
}


def _ist_ignoriert(ueberschrift: str) -> bool:
    """True wenn der Abschnitt ignoriert werden soll, False wenn er mitgenommen wird."""
    ueberschrift_lower = ueberschrift.lower()
    for ignorier_kw in ABSCHNITT_IGNORIEREN:
        if ignorier_kw in ueberschrift_lower:
            return True
    return False


def _compat_kategorie(ueberschrift: str) -> str | None:
    """Gibt Kategorie für Backward-Compat zurück (kurzfassung/warnhinweise/dosierung/None)."""
    ueberschrift_lower = ueberschrift.lower()
    for kategorie, keywords in _COMPAT_KEYWORDS.items():
        for kw in keywords:
            if kw in ueberschrift_lower:
                return kategorie
    return None


def _html_zu_text(element) -> str:
    """Extrahiert sauberen Plaintext aus einem lxml-Element."""
    rohtext = element.text_content()
    text = re.sub(r"\s+", " ", rohtext).strip()
    return text


def parse_beipackzettel(lokale_datei: str) -> dict:
    """
    Liest eine lokale HTML-Beipackzettel-Datei und gibt die relevanten
    Abschnitte als strukturierten Dict zurück.

    Rückgabe:
        {
            "medikament":         "DAFALGAN® Tabletten",
            "relevanter_rohtext": "...",   # Schicht-1-Output: alle relevanten Abschnitte
                                           # mit [Überschriften] als Kontext-Marker
            # Backward-Compat für /api/generate Fallback:
            "kurzfassung_roh":    "...",
            "warnhinweise_roh":   "...",
            "dosierung_roh":      "...",
        }
    """
    dateipfad = config.HTML_DIR / lokale_datei
    if not dateipfad.exists():
        raise FileNotFoundError(f"Beipackzettel nicht gefunden: {dateipfad}")

    inhalt = dateipfad.read_bytes()
    baum = lxml_html.document_fromstring(inhalt)

    title_elemente = baum.findall(".//title")
    medikament = title_elemente[0].text_content().strip() if title_elemente else lokale_datei

    alle_p = baum.findall(".//p")

    relevante_teile: list[str] = []          # Schicht-1-Output
    compat: dict[str, list[str]] = {k: [] for k in _COMPAT_KEYWORDS}

    aktiv: bool = False                       # True = aktueller Abschnitt wird mitgenommen
    compat_kategorie: str | None = None       # Kategorie für Backward-Compat

    for p in alle_p:
        abschnitt_id = p.get("id", "")
        ist_ueberschrift = abschnitt_id.startswith("section")

        if ist_ueberschrift:
            ueberschrift = _html_zu_text(p)
            aktiv = not _ist_ignoriert(ueberschrift)
            compat_kategorie = _compat_kategorie(ueberschrift)
            if aktiv:
                relevante_teile.append(f"[{ueberschrift}]")
            continue

        if not aktiv:
            continue

        text = _html_zu_text(p)
        if not text:
            continue

        relevante_teile.append(text)

        if compat_kategorie is not None:
            compat[compat_kategorie].append(text)

    return {
        "medikament":         medikament,
        "relevanter_rohtext": " ".join(relevante_teile),
        # Backward-Compat
        "kurzfassung_roh":    " ".join(compat["kurzfassung"]),
        "warnhinweise_roh":   " ".join(compat["warnhinweise"]),
        "dosierung_roh":      " ".join(compat["dosierung"]),
    }


def medikament_suchen(suchbegriff: str) -> list[dict]:
    """Sucht im mapping.json nach Einträgen die den Suchbegriff enthalten."""
    import json

    if not config.MAPPING_FILE.exists():
        return []

    with open(config.MAPPING_FILE, encoding="utf-8") as f:
        mapping = json.load(f)

    suchbegriff_lower = suchbegriff.lower()
    treffer = []

    for kategorie, eintraege in mapping.items():
        if not isinstance(eintraege, list):
            continue
        for eintrag in eintraege:
            beschreibung = eintrag.get("beschreibung", "")
            if suchbegriff_lower in beschreibung.lower():
                treffer.append({"kategorie": kategorie, **eintrag})

    return treffer


def eintrag_nach_swissmedic_id(swissmedic_id: str) -> dict | None:
    """Sucht im mapping.json nach einem Eintrag mit der gegebenen Swissmedic-ID."""
    import json

    if not config.MAPPING_FILE.exists():
        return None

    with open(config.MAPPING_FILE, encoding="utf-8") as f:
        mapping = json.load(f)

    for kategorie, eintraege in mapping.items():
        if not isinstance(eintraege, list):
            continue
        for eintrag in eintraege:
            if swissmedic_id in eintrag.get("swissmedic_ids", []):
                return {"kategorie": kategorie, **eintrag}

    return None
