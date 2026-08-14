# HA Sourdough Monitor

Ett lokalt Home Assistant-tillägg som övervakar en surdegsstarter med en nätverkskamera, OpenCV och MQTT Discovery. Tillägget innehåller även en bakjournal och skapar keyframes samt timelapse-videor.

## Installation som add-on repository

1. Installera och konfigurera **Mosquitto broker** i Home Assistant.
2. Öppna **Inställningar → Tillägg → Tilläggsbutiken**.
3. Öppna menyn uppe till höger och välj **Repositories**.
4. Lägg till `https://github.com/ArvidSagbrant/HA-Sourdough-Monitor`.
5. Installera **Sourdough Monitor** från den nya repository-sektionen.
6. Ange kamerans URL i add-on-konfigurationen och starta tillägget.
7. Öppna webbgränssnittet via **Öppna webbgränssnitt**.

MQTT-anslutningen hämtas automatiskt från Home Assistants MQTT-tjänst. De manuella `mqtt_*`-inställningarna behöver bara användas med en extern broker.

## Lokal installation

Kopiera mappen `sourdough_monitor` till `/addons/sourdough_monitor` på Home Assistant-värden. Ladda sedan om lokala tillägg i Tilläggsbutiken.

## Kamera

Tillägget stöder snapshot-URL och RTSP, separat användarnamn och lösenord samt TCP eller UDP för RTSP. Använd fliken **Kamera & ROI** för att kontrollera bilden och markera området som ska analyseras.

## Data och media

- Journalen sparas beständigt i `/data/sourdough_journal.db`.
- ROI-justeringar sparas i `/data/roi.json`.
- Bilder och timelapse sparas under `/media/sourdough`.
- Äldre sessioner begränsas med inställningen `keep_sessions`.

## MQTT-entiteter

MQTT Discovery skapar sensorer för tillväxt, höjd, status, session och aktivt bak samt knappar för att starta, stoppa och bygga timelapse.

## Utveckling

Add-on-filerna finns i `sourdough_monitor/`. `repository.yaml` gör repositoryt kompatibelt med Home Assistants add-on store.
