import os
from pathlib import Path

# Verzeichnisse
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
HTML_DIR = DATA_DIR / "html"
ICONS_DIR = BASE_DIR / "icons"
LOGS_DIR = BASE_DIR / "logs"
MAPPING_FILE = DATA_DIR / "mapping.json"
AIPS_XML = DATA_DIR / "AipsDownload_20260327.xml"

# Claude API
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-6"

# Gemini API via ADC (Application Default Credentials)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_IMAGE_MODEL = "gemini-3.1-flash-image"
GEMINI_GCP_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "")

# XML-Namespace der Swissmedicinfo-Datei
AIPS_NAMESPACE = "https://simisinfo.refdata.ch/MedicinalDocuments/1.0/"

# Medikamente für den Prototyp.
# Neues Medikament hinzufügen: Suchbegriff (Kleinbuchstaben) in die Liste eintragen,
# dann scripts/setup_medikamente.py ausführen.
MEDIKAMENTE = [
    "dafalgan",
    "ibuprofen",
    "pantoprazol",
    "irfen",
]

# Darstellungs-Regler (MVP): 0 = reiner Text, 5 = maximal visuell
REGLER_MIN = 0
REGLER_MAX = 5
REGLER_DEFAULT = 2

# ---------------------------------------------------------------------------
# USP-Piktogramm-Mapping (US Pharmacopeia, standardisierte Medikamenten-
# piktogramme). Semantischer Name → relativer Pfad ab ICONS_DIR.
# Claude erhält zu jedem Namen eine Beschreibung (in prompt_builder.py).
# Neue Piktogramme: EPS-Datei in icons/pictoeps/ ablegen,
# scripts/convert_picto.sh ausführen, dann hier eintragen.
# ---------------------------------------------------------------------------

ICON_KATEGORIEN = {
    "warnhinweise": [
        "schwangerschaft", "stillen", "kein_fahren", "schwindel_warnung",
        "nicht_zerkleinern", "keine_milch", "kein_rauchen", "keine_anderen_meds",
    ],
    "dosierung": [
        "oral", "2x_taeglich", "3x_taeglich", "4x_taeglich",
        "mit_mahlzeit", "ohne_mahlzeit", "1h_vor_mahlzeit", "1h_nach_mahlzeit",
        "vor_schlaf", "mit_wasser", "kauen", "nicht_kauen",
        "unter_zunge", "in_wasser_aufloesen",
    ],
}

ICON_MAPPING = {
    # Einnahme – Art
    "oral":                 "picto/01 [Konvertiert].svg",
    "kauen":                "picto/43 [Konvertiert].svg",
    "nicht_kauen":          "picto/48 [Konvertiert].svg",
    "unter_zunge":          "picto/46 [Konvertiert].svg",
    "in_wasser_aufloesen":  "picto/45 [Konvertiert].svg",

    # Einnahme – Häufigkeit
    "2x_taeglich":          "picto/04 [Konvertiert].svg",
    "3x_taeglich":          "picto/16 [Konvertiert].svg",
    "4x_taeglich":          "picto/15 [Konvertiert].svg",

    # Einnahme – Zeitpunkt / Bezug zur Mahlzeit
    "mit_mahlzeit":         "picto/18 [Konvertiert].svg",
    "ohne_mahlzeit":        "picto/19 [Konvertiert].svg",
    "1h_vor_mahlzeit":      "picto/05 [Konvertiert].svg",
    "1h_nach_mahlzeit":     "picto/06 [Konvertiert].svg",
    "vor_schlaf":           "picto/22 [Konvertiert].svg",

    # Einnahme – Begleitung
    "mit_wasser":           "picto/38 [Konvertiert].svg",

    # Warnungen (nur Icons mit vorhandener SVG-Datei)
    "schwangerschaft":      "picto/34 [Konvertiert].svg",
    "stillen":              "picto/36 [Konvertiert].svg",
    "kein_fahren":          "picto/50 [Konvertiert].svg",
    "schwindel_warnung":    "picto/47 [Konvertiert].svg",
    "nicht_zerkleinern":    "picto/33 [Konvertiert].svg",
    "keine_milch":          "picto/23 [Konvertiert].svg",
    "kein_rauchen":         "picto/55 [Konvertiert].svg",
    "keine_anderen_meds":   "picto/70 [Konvertiert].svg",
}
