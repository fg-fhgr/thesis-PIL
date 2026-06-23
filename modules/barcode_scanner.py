"""
barcode_scanner.py
------------------
Liest Barcodes aus einem Bild (pyzbar + OpenCV) und konvertiert
GTIN-13-Codes in Swissmedic-IDs für den mapping.json-Lookup.

Öffentliche Einstiegsfunktionen für app.py:
  eintrag_fuer_barcode_string()  – Lookup in mapping.json, Fallback über
                                   Refdata-Index + AIPS-XML (alle ~17k Produkte)
  eintrag_dynamisch_laden()      – lädt PIL aus AIPS-XML und trägt ins Mapping ein
"""

import json
import re
import urllib.request
from pathlib import Path

import config
from modules.xml_parser import eintrag_nach_swissmedic_id

# GTIN → Swissmedic-Produkt-ID, wird beim ersten Aufruf einmalig aus Refdata XML geladen
_gtin_index: dict | None = None
_REFDATA_XML = config.DATA_DIR / "Refdata.Articles" / "Refdata.Articles.xml"
_REFDATA_NS = "https://simisinfo.refdata.ch/Articles/1.0/"


def _lade_gtin_index() -> dict:
    """
    Parst Refdata.Articles.xml einmalig (lazy, ~17k Einträge, <2 MB RAM)
    und gibt ein Dict {gtin_string: swissmedic_produkt_id} zurück.
    """
    global _gtin_index
    if _gtin_index is not None:
        return _gtin_index

    _gtin_index = {}
    if not _REFDATA_XML.exists():
        return _gtin_index

    try:
        from lxml import etree
        ns = _REFDATA_NS
        context = etree.iterparse(
            str(_REFDATA_XML), events=("end",), tag=f"{{{ns}}}Article"
        )
        for _, article in context:
            med = article.find(f"{{{ns}}}MedicinalProduct")
            pkg = article.find(f"{{{ns}}}PackagedProduct")
            if med is not None and pkg is not None:
                sid = med.findtext(f"{{{ns}}}RegulatedAuthorisationIdentifier")
                gtin = pkg.findtext(f"{{{ns}}}DataCarrierIdentifier")
                if sid and gtin:
                    _gtin_index[gtin] = sid
            article.clear()
    except Exception:
        pass

    return _gtin_index


def barcode_aus_bild_lesen(bild_bytes: bytes) -> str | None:
    """
    Dekodiert Barcodes aus einem Bild (als Bytes).
    Gibt den ersten gefundenen Barcode-String zurück oder None.
    """
    try:
        import io
        from PIL import Image
        from pyzbar.pyzbar import decode
    except ImportError:
        return None

    try:
        bild = Image.open(io.BytesIO(bild_bytes)).convert("RGB")
        for barcode in decode(bild):
            return barcode.data.decode("utf-8")
        return None
    except Exception:
        return None


def gtin_zu_swissmedic_id(gtin_roh: str) -> str | None:
    """
    Konvertiert eine GTIN in eine Swissmedic-Produkt-ID.

    Schritt 1: mapping.json-Lookup (schnell, bereits gecachte Medikamente).
    Schritt 2: Refdata.Articles.xml-Index (alle ~17k Schweizer Pharmaprodukte).

    Gibt die Swissmedic-ID zurück oder None wenn die GTIN unbekannt ist.
    """
    ziffern = "".join(c for c in gtin_roh if c.isdigit())

    if not ziffern:
        return None

    # Kurzform: bereits eine Swissmedic-ID (5–8 Stellen)
    if len(ziffern) <= 8:
        if eintrag_nach_swissmedic_id(ziffern):
            return ziffern
        return None

    # Schritt 1: mapping.json — verschiedene Teilstring-Varianten
    if len(ziffern) == 13 and ziffern.startswith("7680"):
        for start, ende in [(4, 9), (4, 12), (4, 11), (4, 10), (4, 8)]:
            kandidat = ziffern[start:ende]
            if eintrag_nach_swissmedic_id(kandidat):
                return kandidat
        # Schweizer GTIN: 5-stellige Swissmedic-ID steckt immer in Stellen 4-9.
        # Direkt zurückgeben damit eintrag_dynamisch_laden sie im AIPS-XML findet —
        # auch wenn das Medikament noch nicht im mapping.json ist.
        return ziffern[4:9]

    for laenge in [8, 7, 6, 5]:
        if len(ziffern) >= laenge:
            kandidat = ziffern[4:4 + laenge] if len(ziffern) >= 4 + laenge else ziffern[-laenge:]
            if eintrag_nach_swissmedic_id(kandidat):
                return kandidat

    # Schritt 2: Refdata-Index (für nicht-7680 GTINs)
    gtin_index = _lade_gtin_index()
    sid = gtin_index.get(ziffern)
    if sid:
        return sid

    return None


def eintrag_fuer_barcode(bild_bytes: bytes) -> dict | None:
    """
    Liest einen Barcode aus dem Bild und gibt den zugehörigen
    mapping.json-Eintrag zurück (Fallback: dynamisch laden).
    """
    gtin_roh = barcode_aus_bild_lesen(bild_bytes)
    if gtin_roh is None:
        return None

    swissmedic_id = gtin_zu_swissmedic_id(gtin_roh)
    if swissmedic_id is None:
        return None

    eintrag = eintrag_nach_swissmedic_id(swissmedic_id)
    if eintrag:
        return eintrag
    return eintrag_dynamisch_laden(swissmedic_id)


def _dateiname_aus_beschreibung(beschreibung: str) -> str:
    """Erstellt einen sicheren Dateinamen aus der Medikamentbeschreibung."""
    name = beschreibung.lower()
    name = re.sub(r"[®©™°]", "", name)
    name = re.sub(r"[^a-z0-9]+", "_", name)
    name = name.strip("_")
    return name[:80] + ".html"


def _mapping_eintragen(eintrag: dict) -> None:
    """Fügt einen Eintrag zu mapping.json hinzu (ohne Duplikate)."""
    mapping: dict = {}
    if config.MAPPING_FILE.exists():
        with open(config.MAPPING_FILE, encoding="utf-8") as f:
            mapping = json.load(f)

    # Schlüssel = erster Swissmedic-ID-Wert (eindeutig genug für den Prototyp)
    schluessel = eintrag["swissmedic_ids"][0] if eintrag["swissmedic_ids"] else "unbekannt"
    kategorie = mapping.setdefault(schluessel, [])

    # Duplikat-Prüfung anhand der lokalen Datei
    bekannte = {e.get("lokale_datei") for e in kategorie}
    if eintrag["lokale_datei"] not in bekannte:
        kategorie.append(eintrag)

    config.MAPPING_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(config.MAPPING_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)


def eintrag_dynamisch_laden(swissmedic_id: str) -> dict | None:
    """
    Sucht eine Swissmedic-ID direkt in der lokalen AIPS-XML, lädt den
    deutschen Beipackzettel (HTML) herunter und trägt das Medikament in
    mapping.json ein.

    Gibt den fertigen Mapping-Eintrag zurück oder None bei Misserfolg.
    """
    try:
        from lxml import etree
    except ImportError:
        return None

    if not config.AIPS_XML.exists():
        return None

    ns = config.AIPS_NAMESPACE

    try:
        tree = etree.parse(str(config.AIPS_XML))
    except Exception:
        return None

    root = tree.getroot()

    for bundle in root.findall(f"{{{ns}}}MedicinalDocumentsBundle"):
        doc_typ = bundle.findtext(f"{{{ns}}}Type")
        if doc_typ != "PIL":
            continue

        reg_auth = bundle.find(f"{{{ns}}}RegulatedAuthorization")
        if reg_auth is None:
            continue

        ids = [el.text for el in reg_auth.findall(f"{{{ns}}}Identifier") if el.text]
        if swissmedic_id not in ids:
            continue

        # Deutschen Beipackzettel-Eintrag suchen
        for attached in bundle.findall(f"{{{ns}}}AttachedDocument"):
            if attached.findtext(f"{{{ns}}}Language") != "de":
                continue

            beschreibung = attached.findtext(f"{{{ns}}}Description") or ""

            html_url = None
            for docref in attached.findall(f"{{{ns}}}DocumentReference"):
                if docref.findtext(f"{{{ns}}}ContentType") == "text/html":
                    url = docref.findtext(f"{{{ns}}}Url")
                    if url:
                        html_url = url
                        break

            if not html_url:
                continue

            lokale_datei = _dateiname_aus_beschreibung(beschreibung)
            zieldatei = config.HTML_DIR / lokale_datei

            # HTML herunterladen (nur wenn noch nicht lokal vorhanden)
            if not zieldatei.exists():
                config.HTML_DIR.mkdir(parents=True, exist_ok=True)
                try:
                    req = urllib.request.Request(
                        html_url, headers={"User-Agent": "Mozilla/5.0"}
                    )
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        zieldatei.write_bytes(resp.read())
                except Exception:
                    return None

            eintrag = {
                "beschreibung": beschreibung,
                "swissmedic_ids": ids,
                "html_url": html_url,
                "lokale_datei": lokale_datei,
            }
            _mapping_eintragen(eintrag)
            return eintrag

    return None


def eintrag_fuer_barcode_string(gtin_roh: str) -> dict | None:
    """
    Wie eintrag_fuer_barcode(), aber direkt mit einem Barcode-String.
    Wird vom Live-Scanner verwendet. Fallback: PIL dynamisch über AIPS laden.
    """
    swissmedic_id = gtin_zu_swissmedic_id(gtin_roh)
    if swissmedic_id is None:
        return None

    eintrag = eintrag_nach_swissmedic_id(swissmedic_id)
    if eintrag:
        return eintrag
    return eintrag_dynamisch_laden(swissmedic_id)
