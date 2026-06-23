# Setup & Start — Beipackzettel-Prototyp v2

## 1. Dependencies installieren (einmalig)

```bash
cd Masterthesis/Prototyp/prototyp-v2
pip install -r requirements.txt
```

## 2. API Key setzen

```bash
cp .env.template .env
# .env öffnen und ANTHROPIC_API_KEY eintragen
```

## 3. HTTPS-Zertifikat erstellen (einmalig — nötig für iPad-Kamera)

```bash
brew install mkcert
mkcert -install

# Deine lokale IP herausfinden:
ipconfig getifaddr en0   # → z.B. 192.168.1.42

# Zertifikat für IP + localhost erstellen:
cd Masterthesis/Prototyp/prototyp-v2
mkcert 192.168.1.42 localhost 127.0.0.1
# Erstellt: 192.168.1.42+2.pem  und  192.168.1.42+2-key.pem

# Umbenennen auf den erwarteten Namen:
mv 192.168.1.42+2.pem cert.pem
mv 192.168.1.42+2-key.pem key.pem
```

## 4. Server starten

```bash
cd Masterthesis/Prototyp/prototyp-v2
python server.py
# → Server läuft auf https://0.0.0.0:5001
```

## 5. iPad verbinden

1. MacBook und iPad im **gleichen WLAN**
2. Safari auf iPad öffnen → `https://192.168.1.42:5001`
   (IP aus Schritt 3 verwenden)
3. Beim SSL-Zertifikat-Warning: **Details → Diese Website besuchen**
   (nur beim ersten Mal)
4. **Zum Home-Bildschirm hinzufügen** (Teilen → Zum Homebildschirm)
5. Kiosk-Modus: **Einstellungen → Bedienungshilfen → Geführter Zugriff** aktivieren

## 6. Dev-Modus (ohne iPad, für lokale Tests)

```bash
open https://localhost:5001
# Klick auf "⚡ Dev: Dafalgan laden" überspringt den Scan
```

---

## Bekannte Hinweise

- **Kamera**: Nur über HTTPS zugänglich (Safari-Sicherheitsanforderung)
- **BarcodeDetector**: Verfügbar ab iOS 17 / Safari 17 — ältere Geräte nutzen automatisch Server-Fallback
- **Gemini-Bilder**: Narrativ-Panels zeigen Platzhalter bis Gemini API Key in `.env` eingetragen ist
- **Reset**: "Neu scannen" Knopf setzt Session zurück
