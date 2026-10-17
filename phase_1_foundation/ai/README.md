# VISIONX Phase 3 — Fall Detection Prototype

This first milestone adds a prerecorded-video test script. It does not modify the existing ESP32 firmware or UDP receiver.

## Current behavior
- Uses Ultralytics `yolo11n-pose.pt` for person pose estimation and ByteTrack IDs.
- Uses a simple posture heuristic plus multi-frame temporal validation to flag likely falls.
- Writes annotated test video, CSV event log, and event evidence images to `phase_1_foundation/recording/fall_tests/<run timestamp>/`.
- If an evidence image cannot be written, it logs the error and continues processing.
- Does **not** send SMS yet. The uploaded firmware currently streams camera frames to the laptop; it does not yet implement a laptop-to-ESP32 alert command receiver or a SIM800L SMS command path. That will be a later integration step.

## Important limitation
This is a prototype heuristic, not a guaranteed fall detector or medical device. Validate it using multiple representative videos before relying on alerts.
