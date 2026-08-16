# HA Sourdough Monitor

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

HA Sourdough Monitor is a local Home Assistant add-on for visually tracking the rise of a sourdough starter with a network camera. It uses OpenCV to detect the top edge of the starter, reports measurements through MQTT Discovery, stores annotated camera frames, and creates timelapse videos.

The add-on also includes a bake journal for keeping recipes, fermentation milestones, results, notes, camera measurements, and photos together in one place.

> [!IMPORTANT]
> This project has been generated and developed primarily with the assistance of AI coding tools, under human direction and review. It should be treated as an experimental hobby project. Review and test changes before relying on them, and do not use the measurements as a substitute for food-safety guidance or your own judgement.

## Features

- Supports HTTP/HTTPS snapshot URLs and RTSP camera streams.
- Supports separate camera credentials and TCP or UDP transport for RTSP.
- Detects the starter's top edge inside a configurable region of interest (ROI).
- Calculates relative growth from the first successful measurement in a session.
- Publishes growth, height, status, elapsed time, frame count, preview images, and session information to Home Assistant through MQTT Discovery.
- Provides Home Assistant buttons for starting monitoring, stopping monitoring, and building a timelapse immediately.
- Includes an interactive detection lab with a live annotated preview.
- Allows camera preview zooming up to 800%, panning, mouse-wheel zoom, and pinch gestures for precise ROI placement.
- Creates annotated frames, start/peak/end keyframes, and H.264 MP4 timelapses.
- Can rebuild the current timelapse periodically while a session is active.
- Automatically resumes an active monitoring session after an add-on or Home Assistant restart.
- Includes a persistent bake journal with:
  - flour, water, starter, and salt quantities;
  - dough temperature, target bulk rise, bulk temperature, and cold-proof duration;
  - an optional Home Assistant temperature sensor with live values and recorded temperature history;
  - a prominent current stage and timestamped stage history;
  - timestamps for starter use, bulk fermentation, proofing, and baking;
  - ratings for oven spring, crumb, crust, and flavour;
  - notes, derived hydration and whole-grain percentages, and measurement summaries;
  - recipe cloning;
  - multiple uploaded photos, captions, and a featured final-loaf image.
- Runs locally as a Home Assistant add-on; no external cloud service is required by the application.

## How it works

1. The add-on retrieves a still image from the configured snapshot or RTSP source.
2. It crops the image to the selected ROI.
3. OpenCV analyses horizontal contrast changes to find the most likely top edge of the starter.
4. The detected starter height is compared with the first measurement of the session, which becomes the 100% baseline.
5. Measurements and an annotated preview are published over MQTT and, when a bake is active, stored in the journal database.
6. Annotated frames are saved and later combined into a timelapse with FFmpeg.

Stable lighting, a fixed camera, a plain background, and a clearly visible starter edge will produce the best results. Reflections, jar markings, shadows, and camera movement can reduce detection accuracy.

## Requirements

- Home Assistant OS or Home Assistant Supervised with add-on support.
- A working MQTT broker and the Home Assistant MQTT integration. The Mosquitto broker add-on is recommended.
- A network camera that provides either:
  - a URL returning a directly decodable image such as JPEG; or
  - an RTSP stream.
- Network access from the add-on to the camera.
- One of the supported architectures: `amd64`, `aarch64`, or `armv7`.

## Installation

### Install from the add-on repository

1. Install and configure an MQTT broker, such as the Mosquitto broker add-on.
2. In Home Assistant, open **Settings → Add-ons → Add-on Store**.
3. Open the menu in the upper-right corner and select **Repositories**.
4. Add this repository:

   ```text
   https://github.com/ArvidSagbrant/HA-Sourdough-Monitor
   ```

5. Find **Sourdough Monitor** in the newly added repository section and install it.
6. Open the add-on's **Configuration** tab and configure the camera.
7. Start the add-on.
8. Enable **Show in sidebar** if desired, then select **Open Web UI**.

### Install as a local add-on

1. Copy the complete `sourdough_monitor` directory to `/addons/sourdough_monitor` on the Home Assistant host.
2. In the Add-on Store, open the upper-right menu and select **Check for updates** to reload local add-ons.
3. Install **Sourdough Monitor** from the **Local add-ons** section.
4. Configure and start it as described above.

## Configuration

The MQTT connection is normally obtained automatically from Home Assistant's MQTT service. Only set the manual `mqtt_*` options when using an external broker.

| Option | Default | Description |
| --- | --- | --- |
| `log_level` | `info` | Logging detail: `error`, `info`, or `debug`. |
| `camera_source` | `snapshot` | Camera input type: `snapshot` or `rtsp`. |
| `camera_url` | `http://192.168.1.100/snapshot.jpg` | Snapshot or RTSP URL. |
| `camera_username` | empty | Optional camera username. |
| `camera_password` | empty | Optional camera password. |
| `rtsp_transport` | `tcp` | RTSP transport: `tcp` or `udp`. |
| `camera_timeout_seconds` | `20` | Maximum wait for one RTSP frame attempt. |
| `camera_retries` | `1` | Extra RTSP frame attempts after a temporary failure. |
| `mqtt_host` | empty | External MQTT host. Leave empty to use Home Assistant's MQTT service. |
| `mqtt_port` | `1883` | External MQTT port. |
| `mqtt_username` | empty | External MQTT username. |
| `mqtt_password` | empty | External MQTT password. |
| `mqtt_tls` | `false` | Enable TLS for the external MQTT connection. |
| `interval_seconds` | `60` | Seconds between measurements while monitoring is active. |
| `roi_x_pct` | `30` | ROI left edge as a percentage of image width. |
| `roi_y_pct` | `10` | ROI top edge as a percentage of image height. |
| `roi_width_pct` | `40` | ROI width as a percentage of image width. |
| `roi_height_pct` | `85` | ROI height as a percentage of image height. |
| `smoothing_frames` | `5` | Number of recent detected edges used for median smoothing. |
| `timelapse_fps` | `30` | Frames per second in the generated timelapse. |
| `timelapse_refresh_minutes` | `30` | Rebuild interval for an active timelapse. Set to `0` to disable periodic rebuilding. |
| `keep_sessions` | `10` | Maximum number of monitoring session directories to retain. |

ROI and detection settings saved in the web UI take effect immediately and override the corresponding add-on configuration values where applicable.

At `error`, only failures are logged. `info` also records normal lifecycle events such as startup, MQTT connection, session start and stop, sourdough status changes, bake phases, and timelapse creation. `debug` additionally logs camera fetch attempts, frame measurements, and web requests. The added log entries do not include camera URLs or MQTT credentials.

## Quick start and basic usage

### 1. Position and configure the camera

Place the camera where it has a fixed, unobstructed view of the starter jar. Keep the camera and jar in the same position for the entire session. Configure `camera_source`, `camera_url`, and any required credentials in the add-on settings, then restart the add-on after changing those settings.

### 2. Calibrate the detection

1. Open the add-on web UI and select **Kamera & detektion** (**Camera & detection**).
2. Select **Uppdatera råbild** (**Refresh raw image**) and confirm that the camera image loads.
3. Drag and resize the cyan ROI so it contains the starter and excludes as much of the jar rim, labels, reflections, and background as possible.
4. Select **Spara ROI** (**Save ROI**).
5. In the detection lab, adjust the search range and contrast direction until the green line follows the starter surface.
6. Select **Spara detektion** (**Save detection**).

The detection preview uses these colours:

- Green: selected starter edge.
- Orange: alternative edge candidates.
- Blue: active vertical search limits.
- Cyan: ROI boundary.

If detection jumps to the jar rim, narrow the ROI or search interval first. Then try a different contrast direction or adjust blur and row smoothing. `max_jump_pct` limits how far the accepted edge may move between measurements; setting it to `0` disables that limit.

### 3. Create a bake journal entry

1. Select **Bakjournal** (**Bake journal**) and create a new bake.
2. Enter the recipe and any known process targets, then save it.
3. Optionally select a Home Assistant temperature sensor. Its live value replaces the manual bulk-temperature field, and readings are saved with camera measurements. Enable **Använd som standardsensor för nya bak** to assign it automatically to future bakes.
4. Make sure the intended bake is marked as active. New bakes become active automatically; an older bake can be selected with **Gör aktivt** (**Make active**).
5. Use the phase buttons as the bake progresses. The current stage and the time of every stage change are shown in the journal.
6. Add notes, result ratings, and process or final-loaf photos when convenient.

Only measurements taken while a bake is active are associated with that bake. Temperature values are recorded on the same interval as the camera measurements. If no temperature sensor is selected, the bulk temperature remains a manual journal field.

### 4. Monitor the starter

1. In Home Assistant, open the MQTT device named **Sourdough Monitor**.
2. Press the **Starta surdeg** button to start a monitoring session.
3. Check the preview, growth sensor, and status sensor while the starter rises.
4. Press **Stoppa och bygg timelapse** when finished.
5. Open Home Assistant's Media browser and browse to the `sourdough` directory to view generated media.

The first detected height in each session is the baseline and is reported as 100%. Start monitoring only after the jar and camera are in their final positions. A session that was active during a restart resumes with its original baseline and frame sequence. A session stopped manually remains stopped.

## Home Assistant entities

MQTT Discovery creates one device with the following entities. Entity IDs may differ if Home Assistant resolves a naming conflict.

| Type | Purpose |
| --- | --- |
| Sensor | Starter growth percentage. |
| Sensor | Detected starter height in pixels. |
| Sensor | Detected top-edge Y coordinate. |
| Sensor | Number of captured frames. |
| Sensor | Elapsed monitoring time in minutes. |
| Sensor | Detection status such as `start`, `rising`, `strong_rise`, `doubled`, or `falling`. |
| Sensor | Current session identifier. |
| Sensor | Active bake and its attributes. |
| Sensor | Timelapse state and media attributes. |
| Binary sensor | Whether monitoring is active. |
| Image | Latest annotated camera preview. |
| Button | Start monitoring. |
| Button | Stop monitoring and build the timelapse. |
| Button | Build the current timelapse immediately. |

All MQTT topics use the `sourdough_monitor` base topic.

## Data and media

Persistent application data is stored in the add-on's `/data` directory:

| Path | Contents |
| --- | --- |
| `/data/sourdough_journal.db` | SQLite bake journal, events, measurements, and active application state. |
| `/data/roi.json` | ROI override saved from the web UI. |
| `/data/detection.json` | Detection settings saved from the web UI. |
| `/data/options.json` | Add-on configuration managed by Home Assistant. |

Media is stored under `/media/sourdough` and is available through Home Assistant's Media browser:

- `latest.jpg` contains the latest annotated frame.
- `latest.mp4` contains the most recently built timelapse.
- `session_YYYYMMDD-HHMMSS/` contains session-specific media.
- `bakes/` contains uploaded journal photos.

When a monitoring session is stopped successfully, its raw frame directory is removed after the timelapse and start/peak/end keyframes have been created. Old session directories are pruned according to `keep_sessions`.

Back up both the add-on data and `/media/sourdough` if the journal and generated media are important to you.

## Development

### Repository structure

```text
.
├── repository.yaml                 # Home Assistant add-on repository metadata
├── README.md
└── sourdough_monitor/
    ├── app.py                      # Monitoring, OpenCV, MQTT, journal API, and web server
    ├── ui.html                     # Dependency-free web UI
    ├── config.yaml                 # Add-on metadata, options, and schema
    ├── build.yaml                  # Architecture-specific base images
    ├── Dockerfile                  # Add-on image
    ├── run.sh                      # Container entry point
    └── CHANGELOG.md
```

The application is intentionally small and currently has no separate build step for the web UI. Python serves `ui.html` and a JSON API on port `8099`; Home Assistant exposes that port through ingress. The HTTP handler only accepts local and Home Assistant ingress-network clients.

### Recommended development workflow

1. Fork and clone the repository.
2. Create a branch for the change.
3. Edit files under `sourdough_monitor/`.
4. Keep the version in `sourdough_monitor/config.yaml` and the `VERSION` constant in `sourdough_monitor/app.py` in sync when preparing a release.
5. Add user-visible changes to `sourdough_monitor/CHANGELOG.md`.
6. Validate the Python source and add-on metadata.
7. Copy or symlink the add-on directory into a Home Assistant development instance and test camera access, MQTT Discovery, ingress, persistence, and media generation.

Useful local checks include:

```bash
python3 -m py_compile sourdough_monitor/app.py
git diff --check
docker build \
  --build-arg BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.22 \
  --tag sourdough-monitor:dev \
  sourdough_monitor
```

Use the matching Home Assistant base image from `sourdough_monitor/build.yaml` when developing on another architecture.

There is currently no automated test suite. At minimum, manually verify:

- snapshot and RTSP camera input as applicable;
- ROI editing, zooming, and detection-preview settings;
- session start, measurement publishing, restart recovery, and stop;
- timelapse and keyframe generation;
- MQTT Discovery entities and commands;
- bake creation, editing, phase timestamps, cloning, and active-bake selection;
- photo upload, featured-photo selection, and deletion;
- persistence after restarting the add-on.

### Optional standalone container smoke test

The add-on can be smoke-tested on a Linux development machine with Docker, an accessible camera, and an external MQTT broker. Create `.dev/data/options.json` containing all options from `sourdough_monitor/config.yaml`, set `mqtt_host` to the external broker, and create `.dev/media`. After building the image, run:

```bash
docker run --rm \
  --network host \
  --volume "$PWD/.dev/data:/data" \
  --volume "$PWD/.dev/media:/media" \
  sourdough-monitor:dev
```

Open `http://127.0.0.1:8099`. Host networking is used because the application limits web access to loopback and the Home Assistant ingress network. This smoke test does not reproduce every Supervisor or ingress behaviour; final testing should be done in Home Assistant.

## Troubleshooting

### The camera preview does not load

- Confirm that the URL returns an image directly when using `snapshot` mode.
- Confirm that the RTSP URL, transport, and credentials are correct when using `rtsp` mode.
- Check that Home Assistant can reach the camera's network and that the camera permits another connection.
- Review the add-on log for HTTP, decoding, FFmpeg, or timeout errors.

### The detected edge is wrong or unstable

- Use a smaller ROI and exclude the jar rim, printed markings, and strong reflections.
- Restrict the detection lab's vertical search range.
- Try the two directional contrast modes instead of `both`.
- Increase blur or row smoothing for noisy images.
- Reduce `max_jump_pct` after the correct edge has been found reliably.
- Improve lighting and avoid automatic camera movement or focus changes.

### MQTT entities do not appear

- Confirm that the MQTT broker and Home Assistant MQTT integration are running.
- Restart the add-on after MQTT becomes available.
- If using an external broker, verify the `mqtt_host`, port, credentials, and TLS setting.
- Check the add-on log for connection errors.

### No timelapse is created

A timelapse requires at least two captured frames. Confirm that monitoring ran long enough for the configured `interval_seconds`, then check the add-on log and available space under `/media`.

## Security and privacy

- Camera images, journal data, and generated media remain on the Home Assistant system unless you expose, back up, or share them through another service.
- Camera and MQTT credentials are stored in the Home Assistant add-on configuration. Protect access to Home Assistant and avoid committing credentials to this repository.
- Prefer a dedicated local camera account with only the permissions the add-on needs.
- Avoid exposing the add-on's internal web port directly to untrusted networks; use Home Assistant ingress.

## Contributing

This is a quick project for my own personal use. No contributions are accepted at this time.

## License

This project is licensed under the [Apache License 2.0](LICENSE). Copyright 2026 Arvid Sagbrant. See [NOTICE](NOTICE) for attribution information.

The license covers the project's own source code and documentation. Third-party dependencies included in or downloaded by the container image remain subject to their respective licenses.
