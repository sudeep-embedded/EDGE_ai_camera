"""VISIONX Phase 3.1: prerecorded-video fall-detection prototype.

Uses the same Ultralytics YOLO pose model + ByteTrack architecture intended for
both prerecorded video and the later ESP32 live-frame input. This first version
is a prototype heuristic, not a medical device or a guaranteed fall detector.

Run from the laptop project's existing Python virtual environment. On first run,
Ultralytics may download yolo11n-pose.pt if it is not already cached.
"""
from __future__ import annotations

import argparse
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog

import cv2
import numpy as np
from ultralytics import YOLO

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "phase_1_foundation" / "recording" / "fall_tests"
DEFAULT_MODEL = "yolo11n-pose.pt"

# COCO pose keypoint indexes.
LEFT_SHOULDER, RIGHT_SHOULDER = 5, 6
LEFT_HIP, RIGHT_HIP = 11, 12

# Temporal validation: candidate must be present in most of the recent frames.
HISTORY_FRAMES = 12
MIN_CANDIDATE_FRAMES = 8
RECOVERY_FRAMES = 18
KEYPOINT_CONFIDENCE = 0.35


def choose_video() -> str:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    selected = filedialog.askopenfilename(
        title="Select a VISIONX fall-detection test video",
        filetypes=[("Video files", "*.mp4 *.avi *.mov *.mkv *.wmv"), ("All files", "*.*")],
    )
    root.destroy()
    return selected


def midpoint(points: np.ndarray, first: int, second: int) -> tuple[float, float] | None:
    if points.shape[0] <= max(first, second):
        return None
    a, b = points[first], points[second]
    if a[2] < KEYPOINT_CONFIDENCE or b[2] < KEYPOINT_CONFIDENCE:
        return None
    return ((float(a[0]) + float(b[0])) / 2, (float(a[1]) + float(b[1])) / 2)


def is_fall_candidate(xy_conf: np.ndarray, box_xyxy: np.ndarray) -> bool:
    """Simple posture heuristic; temporal validation happens separately."""
    shoulders = midpoint(xy_conf, LEFT_SHOULDER, RIGHT_SHOULDER)
    hips = midpoint(xy_conf, LEFT_HIP, RIGHT_HIP)
    if shoulders is None or hips is None:
        return False

    dx = hips[0] - shoulders[0]
    dy = hips[1] - shoulders[1]
    torso_length = float(np.hypot(dx, dy))
    if torso_length < 12:
        return False

    # 0 degrees means the shoulder-to-hip axis is horizontal; 90 is vertical.
    torso_angle_from_horizontal = float(np.degrees(np.arctan2(abs(dy), abs(dx))))
    x1, y1, x2, y2 = [float(v) for v in box_xyxy]
    box_w = max(1.0, x2 - x1)
    box_h = max(1.0, y2 - y1)
    aspect_ratio = box_w / box_h

    # Requiring a relatively horizontal torso plus a broad person box helps
    # reject many ordinary bends, but cannot eliminate all false positives.
    return torso_angle_from_horizontal <= 32.0 and aspect_ratio >= 0.65


def main() -> int:
    parser = argparse.ArgumentParser(description="VISIONX prerecorded-video fall detection")
    parser.add_argument("video", nargs="?", help="Video path; if omitted, a file picker opens")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ultralytics pose model weights")
    parser.add_argument("--confidence", type=float, default=0.35, help="Person detection confidence")
    parser.add_argument("--no-loop", action="store_true", help="Play the selected video only once")
    parser.add_argument("--max-display-width", type=int, default=1100, help="Maximum preview width in pixels")
    parser.add_argument("--max-display-height", type=int, default=700, help="Maximum preview height in pixels")
    args = parser.parse_args()

    video_path = args.video or choose_video()
    if not video_path:
        print("No video selected. Nothing was changed.")
        return 1
    video_path = str(Path(video_path).expanduser().resolve())
    if not Path(video_path).is_file():
        print(f"ERROR: Video file not found: {video_path}")
        return 2

    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = DEFAULT_OUTPUT_ROOT / run_stamp
    evidence_dir = output_dir / "fall_evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    annotated_path = output_dir / "annotated_fall_test.mp4"
    event_log_path = output_dir / "fall_events.csv"

    print("=" * 68)
    print("VISIONX | FALL DETECTION VIDEO TEST")
    print(f"Input video : {video_path}")
    print(f"Model       : {args.model}")
    print(f"Output      : {output_dir}")
    print("Mode        : prerecorded video; laptop evidence storage only")
    print(f"Loop        : {not args.no_loop} (press q or Esc to stop)")
    print("SMS         : not connected in this first milestone")
    print("=" * 68)
    print("Loading YOLO pose model... first run may download model weights.")
    model = YOLO(args.model)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"ERROR: OpenCV could not open video: {video_path}")
        return 3

    fps = float(cap.get(cv2.CAP_PROP_FPS))
    if not np.isfinite(fps) or fps < 1 or fps > 240:
        fps = 25.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if width <= 0 or height <= 0:
        cap.release()
        print("ERROR: Video has invalid dimensions.")
        return 4

    writer = cv2.VideoWriter(
        str(annotated_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height)
    )
    if not writer.isOpened():
        cap.release()
        print("ERROR: Could not create annotated MP4 output.")
        return 5

    histories: dict[int, deque[bool]] = defaultdict(lambda: deque(maxlen=HISTORY_FRAMES))
    active_falls: set[int] = set()
    recovery_count: dict[int, int] = defaultdict(int)
    event_count = 0
    frame_index = 0
    started = time.perf_counter()
    seen_event_keys: set[tuple[int, int]] = set()
    loop_count = 0
    log_file = event_log_path.open("w", encoding="utf-8", newline="")
    log_file.write("event_id,video_time_seconds,track_id,evidence_image\n")

    cv2.namedWindow("VISIONX - Fall Detection Video Test", cv2.WINDOW_NORMAL)
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                if args.no_loop:
                    break
                # Rewind for continuous playback. Clear temporal state so a
                # partial posture history from the previous pass is not reused.
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                histories.clear()
                active_falls.clear()
                recovery_count.clear()
                frame_index = 0
                loop_count += 1
                print(f"Loop {loop_count + 1}: replaying video (press q or Esc to stop)")
                continue
            frame_index += 1
            video_time = frame_index / fps

            # Single shared inference/tracking call. The live mode will feed
            # frames into the same processing function in a later milestone.
            result = model.track(
                frame,
                persist=True,
                tracker="bytetrack.yaml",
                classes=[0],
                conf=args.confidence,
                verbose=False,
            )[0]
            # Use our own compact labels below; hide YOLO labels to avoid overlap.
            annotated = result.plot(labels=False, conf=False)
            current_ids: set[int] = set()

            if result.boxes is not None and len(result.boxes) > 0 and result.keypoints is not None:
                boxes = result.boxes.xyxy.cpu().numpy()
                keypoints = result.keypoints.data.cpu().numpy()
                ids_tensor = result.boxes.id
                track_ids = (
                    ids_tensor.int().cpu().tolist()
                    if ids_tensor is not None
                    else [-(i + 1) for i in range(len(boxes))]
                )

                for index, (box, pose, track_id) in enumerate(zip(boxes, keypoints, track_ids)):
                    if track_id < 0:
                        continue
                    current_ids.add(track_id)
                    candidate = is_fall_candidate(pose, box)
                    history = histories[track_id]
                    history.append(candidate)
                    positive_count = sum(history)

                    x1, y1, x2, y2 = [int(v) for v in box]
                    label = f"ID {track_id}"
                    if track_id in active_falls:
                        label += " | FALL ALERT"
                        recovery_count[track_id] = 0 if candidate else recovery_count[track_id] + 1
                        if recovery_count[track_id] >= RECOVERY_FRAMES:
                            active_falls.remove(track_id)
                            recovery_count[track_id] = 0
                    elif len(history) == HISTORY_FRAMES and positive_count >= MIN_CANDIDATE_FRAMES:
                        active_falls.add(track_id)
                        recovery_count[track_id] = 0
                        # Do not create duplicate events for the same approximate
                        # video timestamp when looping over the same clip.
                        event_key = (int(round(video_time * fps)), int(track_id))
                        if event_key in seen_event_keys:
                            active_falls.discard(track_id)
                            continue
                        seen_event_keys.add(event_key)
                        event_count += 1
                        label += " | FALL ALERT"
                        evidence_path = evidence_dir / f"fall_{event_count:04d}_id_{track_id}.jpg"
                        # Storage failures are logged and never stop the video loop.
                        try:
                            saved = cv2.imwrite(str(evidence_path), annotated)
                            if not saved:
                                raise OSError("OpenCV imwrite returned False")
                            evidence_value = str(evidence_path)
                        except Exception as exc:  # noqa: BLE001 - keep pipeline running
                            evidence_value = "STORAGE_ERROR"
                            print(f"WARNING: evidence save failed; continuing: {exc}")
                        log_file.write(
                            f"{event_count},{video_time:.3f},{track_id},\"{evidence_value}\"\n"
                        )
                        log_file.flush()
                        print(
                            f"FALL EVENT #{event_count} | video time={video_time:.2f}s "
                            f"| track ID={track_id} | evidence={evidence_value}"
                        )

                    cv2.putText(
                        annotated,
                        label,
                        (max(5, x1), max(22, y1 - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.65,
                        (0, 0, 255) if track_id in active_falls else (0, 220, 0),
                        2,
                        cv2.LINE_AA,
                    )

            # Overlay test status and write every processed frame.
            cv2.putText(
                annotated,
                f"VISIONX FALL TEST | frame {frame_index} | events {event_count}",
                (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2, cv2.LINE_AA,
            )
            writer.write(annotated)
            display_scale = min(
                args.max_display_width / width,
                args.max_display_height / height,
                1.0,
            )
            if display_scale < 1.0:
                display_frame = cv2.resize(
                    annotated, (int(width * display_scale), int(height * display_scale)),
                    interpolation=cv2.INTER_AREA,
                )
            else:
                display_frame = annotated
            cv2.imshow("VISIONX - Fall Detection Video Test", display_frame)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                print("Test stopped by user; partial output has been retained.")
                break

    finally:
        cap.release()
        writer.release()
        log_file.close()
        cv2.destroyAllWindows()

    elapsed = time.perf_counter() - started
    print("\nTest complete.")
    print(f"Frames processed : {frame_index}")
    print(f"Fall events      : {event_count}")
    print(f"Processing time  : {elapsed:.1f}s")
    print(f"Annotated video  : {annotated_path}")
    print(f"Event log        : {event_log_path}")
    print(f"Evidence folder  : {evidence_dir}")
    print("Note: this heuristic needs validation on varied videos; it may miss falls or flag similar postures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
