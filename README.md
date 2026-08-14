# Sourdough Monitor v0.2.0

Adds an integrated bake journal to the existing Home Assistant local add-on.

## New
- SQLite journal in `/data/sourdough_journal.db`
- Bake IDs such as `2026-08-13-01`
- Recipe, process, phase timestamps, notes and 1–5 result ratings
- Clone previous recipe
- Mark one bake as active
- Camera measurements are written to the active bake automatically
- Current starter session and generated keyframes/timelapse are linked to the active bake
- Ingress UI now has `Bakjournal` and `Kamera & ROI` tabs
- MQTT Discovery adds `Aktivt bak`

## Upgrade
Replace the files in `/addons/sourdough_monitor/` with this directory, reload local add-ons/apps, then rebuild/reinstall or restart the add-on as appropriate for your Home Assistant installation.

Existing `/media/sourdough` sessions are not deleted by the upgrade. The journal database is created in `/data`, so it persists across container restarts/rebuilds.
