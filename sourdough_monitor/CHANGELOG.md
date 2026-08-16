# Changelog

## 0.9.1

- Added automatic retry of transient RTSP frame failures.
- Camera timeouts now keep the monitoring session active without noisy tracebacks.
- RTSP timeout errors no longer expose the authenticated camera URL in logs.
- Added configurable camera timeout and retry counts.

## 0.9.0

- Added a Home Assistant temperature-sensor selector to every bake.
- Added a persistent default temperature sensor that is automatically assigned to new bakes.
- Live sensor temperature now replaces the manual bulk-temperature display when a sensor is selected.
- Temperature readings are stored together with monitoring measurements, including min, average, and max summaries.
- Added a prominent current-stage indicator, timestamped phase controls, and a stage history for every bake.
- Added current bake stage and stage-change time to the active-bake MQTT attributes.

## 0.8.0

- Added solid black backgrounds behind OpenCV overlay labels for consistent text contrast.
- Added black outlines behind ROI, detection, candidate, and search-boundary lines so they remain visible on both light and dark images.

## 0.7.0

- Added photo uploads to each bake, with optional captions and support for multiple process photos.
- Added a featured final-loaf photo and a responsive bake gallery.
- Added controls to promote another photo or permanently remove a photo.

## 0.6.0

- Active monitoring sessions now resume automatically after an add-on or Home Assistant restart.
- Session timing, baseline, frame numbering, detection history, and keyframe tracking are preserved.
- Sessions stopped deliberately remain stopped after a restart.

## 0.5.0

- Added camera preview zoom up to 800% using toolbar buttons, the mouse wheel, or pinch gestures.
- Added panning for precise ROI placement while zoomed in.
- Added a fit-to-image control and visible zoom level indicator.
- Kept ROI coordinates independent of the preview zoom level.

## 0.4.0

- Added an interactive OpenCV detection tuning lab with live visual feedback.
- Added persistent detection settings and configurable edge search bounds.
- Added protection against implausible edge jumps between measurements.

## 0.3.0

- Added the sourdough bake journal, camera measurements, keyframes, and timelapse generation.
