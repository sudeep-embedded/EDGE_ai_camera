"""VISIONX shared fall-detection pipeline for video and live camera frames.

Storage layout:
recording/
    video_testing/
        classroom/
        patient/
    real_time/
        classroom/
        patient/

Each validated fall gets its own evidence.jpg and event_info.json.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RECORDING_ROOT = PROJECT_ROOT / "phase_1_foundation" / "recording"

DEFAULT_MODEL = (
    str(PROJECT_ROOT / "yolo11n-pose.pt")
    if (PROJECT_ROOT / "yolo11n-pose.pt").is_file()
    else "yolo11n-pose.pt"
)

LEFT_SHOULDER, RIGHT_SHOULDER = 5, 6
LEFT_HIP, RIGHT_HIP = 11, 12

HISTORY_FRAMES = 12
MIN_CANDIDATE_FRAMES = 8
RECOVERY_FRAMES = 18
KEYPOINT_CONFIDENCE = 0.35

DOMAIN_NAMES = {
    "classroom": "Classroom Intelligence",
    "patient": "Patient Monitoring & Safety (Hospital)",
}

MODE_NAMES = {
    "video": "video_testing",
    "video_testing": "video_testing",
    "live": "real_time",
    "real_time": "real_time",
}


def midpoint(points: np.ndarray, first: int, second: int):
    """Return the midpoint of two sufficiently confident pose keypoints."""
    if points.shape[0] <= max(first, second):
        return None

    a, b = points[first], points[second]

    if a[2] < KEYPOINT_CONFIDENCE or b[2] < KEYPOINT_CONFIDENCE:
        return None

    return (
        (float(a[0]) + float(b[0])) / 2,
        (float(a[1]) + float(b[1])) / 2,
    )


def is_fall_candidate(xy_conf: np.ndarray, box_xyxy: np.ndarray) -> bool:
    """Heuristic fall candidate based on pose geometry and bounding box."""
    shoulders = midpoint(xy_conf, LEFT_SHOULDER, RIGHT_SHOULDER)
    hips = midpoint(xy_conf, LEFT_HIP, RIGHT_HIP)

    if shoulders is None or hips is None:
        return False

    dx = hips[0] - shoulders[0]
    dy = hips[1] - shoulders[1]

    if float(np.hypot(dx, dy)) < 12:
        return False

    angle = float(np.degrees(np.arctan2(abs(dy), abs(dx))))

    x1, y1, x2, y2 = [float(v) for v in box_xyxy]
    aspect_ratio = max(1.0, x2 - x1) / max(1.0, y2 - y1)

    return angle <= 32.0 and aspect_ratio >= 0.65


def next_event_number(event_root: Path) -> int:
    """Return the next event number for this mode and domain only."""
    highest = 0

    if event_root.exists():
        for path in event_root.glob("Fall_*"):
            if path.is_dir():
                try:
                    number = int(path.name.split("_", 1)[1])
                    highest = max(highest, number)
                except (IndexError, ValueError):
                    continue

    return highest + 1


class FallDetector:
    """Shared YOLO pose detector/tracker for video and live camera frames."""

    def __init__(
        self,
        model_path: str = DEFAULT_MODEL,
        confidence: float = 0.35,
        domain: str = "classroom",
        input_mode: str = "video_testing",
    ):
        if domain not in DOMAIN_NAMES:
            raise ValueError(f"Unsupported VISIONX domain: {domain!r}")

        if input_mode not in MODE_NAMES:
            raise ValueError(
                "input_mode must be 'video_testing', 'real_time', "
                "'video', or 'live'."
            )

        self.model_path = model_path
        self.confidence = confidence
        self.domain = domain
        self.domain_name = DOMAIN_NAMES[domain]
        self.input_mode = MODE_NAMES[input_mode]

        # Exact structure: recording/<mode>/<domain>/
        self.event_root = RECORDING_ROOT / self.input_mode / self.domain
        self.event_root.mkdir(parents=True, exist_ok=True)

        # Each run receives its own session directory.
        session_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        sessions_root = self.event_root / "sessions"
        self.output_dir = sessions_root / session_stamp

        suffix = 1
        while self.output_dir.exists():
            self.output_dir = sessions_root / f"{session_stamp}_{suffix}"
            suffix += 1

        self.output_dir.mkdir(parents=True, exist_ok=True)

        print(f"Loading YOLO pose model: {model_path}")
        self.model = YOLO(model_path)

        self.histories = defaultdict(lambda: deque(maxlen=HISTORY_FRAMES))
        self.active_falls: set[int] = set()
        self.recovery_count = defaultdict(int)

        self.event_count = 0
        self.next_event_id = next_event_number(self.event_root)

        self.event_log_path = self.output_dir / "fall_events.csv"
        self.log_file = None
        self.csv_writer = None

        # If session-log storage fails, keep AI processing where possible.
        try:
            self.log_file = self.event_log_path.open(
                "w", encoding="utf-8", newline=""
            )
            self.csv_writer = csv.writer(self.log_file)
            self.csv_writer.writerow([
                "event_id",
                "mode",
                "domain",
                "source",
                "time_seconds",
                "frame_index",
                "track_id",
                "event_folder",
                "evidence_image",
                "metadata_json",
                "storage_status",
            ])
            self.log_file.flush()
        except OSError as exc:
            print(
                "WARNING: Could not create session CSV log. "
                f"AI will continue: {exc}"
            )
            if self.log_file is not None:
                try:
                    self.log_file.close()
                except OSError:
                    pass
            self.log_file = None
            self.csv_writer = None

        self.closed = False

        print(f"STORAGE MODE : {self.input_mode}")
        print(f"STORAGE FIELD: {self.domain}")
        print(f"EVENT FOLDER : {self.event_root}")
        print(f"SESSION      : {self.output_dir}")

    def reset_temporal_state(self):
        """Reset temporal state when a test video starts another replay."""
        self.histories.clear()
        self.active_falls.clear()
        self.recovery_count.clear()

        # Reset the Ultralytics tracking predictor for a fresh video pass.
        try:
            self.model.predictor = None
        except Exception:
            pass

    def _save_event_evidence(
        self,
        annotated: np.ndarray,
        *,
        event_id: int,
        source: str,
        time_seconds: float,
        frame_index: int,
        track_id: int,
    ) -> dict:
        event_folder = self.event_root / f"Fall_{event_id:04d}"
        evidence_path = event_folder / "evidence.jpg"
        metadata_path = event_folder / "event_info.json"

        storage_status = "SAVED"

        try:
            # Do not overwrite a previously stored event.
            event_folder.mkdir(parents=False, exist_ok=False)

            if not cv2.imwrite(str(evidence_path), annotated):
                raise OSError("OpenCV could not write evidence.jpg")

            metadata = {
                "event_id": event_id,
                "event_type": "fall",
                "mode": self.input_mode,
                "domain": self.domain,
                "domain_name": self.domain_name,
                "source": source,
                "time_seconds": round(float(time_seconds), 3),
                "frame_index": int(frame_index),
                "track_id": int(track_id),
                "detected_at": datetime.now().astimezone().isoformat(
                    timespec="seconds"
                ),
                "event_folder": str(event_folder),
                "evidence_image": str(evidence_path),
                "metadata_json": str(metadata_path),
                "storage_status": storage_status,
                "note": (
                    "Prototype visual fall-detection heuristic. "
                    "Validate under representative conditions. "
                    "An individual event video clip is not generated yet."
                ),
            }

            metadata_path.write_text(
                json.dumps(metadata, indent=2),
                encoding="utf-8",
            )

        except Exception as exc:
            storage_status = f"ERROR: {exc}"
            print(
                "WARNING: Could not save fall evidence; "
                f"AI processing will continue: {exc}"
            )

            # Try to remove a partially created event folder.
            # Do not remove anything if the folder already existed.
            try:
                if event_folder.exists():
                    if evidence_path.exists() and not metadata_path.exists():
                        evidence_path.unlink(missing_ok=True)
                    if not any(event_folder.iterdir()):
                        event_folder.rmdir()
            except OSError:
                pass

        return {
            "event_folder": str(event_folder),
            "evidence_path": (
                str(evidence_path)
                if storage_status == "SAVED"
                else "STORAGE_ERROR"
            ),
            "metadata_path": (
                str(metadata_path)
                if storage_status == "SAVED"
                else "STORAGE_ERROR"
            ),
            "storage_status": storage_status,
        }

    def _write_event_log(self, row: list) -> None:
        """Write the event row without letting log failures stop AI."""
        if self.csv_writer is None or self.log_file is None:
            return

        try:
            self.csv_writer.writerow(row)
            self.log_file.flush()
        except OSError as exc:
            print(f"WARNING: Could not write event CSV row: {exc}")
            try:
                self.log_file.close()
            except OSError:
                pass
            self.log_file = None
            self.csv_writer = None

    def process_frame(
        self,
        frame: np.ndarray,
        *,
        source: str,
        time_seconds: float,
        frame_index: int,
    ):
        """Return (annotated_frame, newly_validated_events)."""
        result = self.model.track(
            frame,
            persist=True,
            tracker="bytetrack.yaml",
            classes=[0],
            conf=self.confidence,
            verbose=False,
        )[0]

        annotated = result.plot(labels=False, conf=False)
        new_events = []

        if (
            result.boxes is not None
            and len(result.boxes) > 0
            and result.keypoints is not None
        ):
            boxes = result.boxes.xyxy.cpu().numpy()
            keypoints = result.keypoints.data.cpu().numpy()
            ids_tensor = result.boxes.id

            if ids_tensor is not None:
                track_ids = ids_tensor.int().cpu().tolist()
            else:
                track_ids = [-(i + 1) for i in range(len(boxes))]

            for box, pose, track_id in zip(boxes, keypoints, track_ids):
                if track_id < 0:
                    continue

                candidate = is_fall_candidate(pose, box)
                history = self.histories[track_id]
                history.append(candidate)

                x1, y1, x2, y2 = [int(v) for v in box]
                label = f"ID {track_id}"

                if track_id in self.active_falls:
                    label += " | FALL ALERT"

                    if candidate:
                        self.recovery_count[track_id] = 0
                    else:
                        self.recovery_count[track_id] += 1

                    if self.recovery_count[track_id] >= RECOVERY_FRAMES:
                        self.active_falls.remove(track_id)
                        self.recovery_count[track_id] = 0

                elif (
                    len(history) == HISTORY_FRAMES
                    and sum(history) >= MIN_CANDIDATE_FRAMES
                ):
                    self.active_falls.add(track_id)
                    self.recovery_count[track_id] = 0

                    event_id = self.next_event_id
                    self.next_event_id += 1
                    self.event_count += 1

                    saved = self._save_event_evidence(
                        annotated,
                        event_id=event_id,
                        source=source,
                        time_seconds=time_seconds,
                        frame_index=frame_index,
                        track_id=track_id,
                    )

                    self._write_event_log([
                        event_id,
                        self.input_mode,
                        self.domain,
                        source,
                        f"{time_seconds:.3f}",
                        frame_index,
                        track_id,
                        saved["event_folder"],
                        saved["evidence_path"],
                        saved["metadata_path"],
                        saved["storage_status"],
                    ])

                    event = {
                        "event_id": event_id,
                        "mode": self.input_mode,
                        "domain": self.domain,
                        "source": source,
                        "time_seconds": time_seconds,
                        "frame_index": frame_index,
                        "track_id": track_id,
                        **saved,
                    }
                    new_events.append(event)

                    print(
                        f"FALL EVENT #{event_id} | "
                        f"mode={self.input_mode} | "
                        f"domain={self.domain} | "
                        f"source={source} | "
                        f"time={time_seconds:.2f}s | "
                        f"track ID={track_id} | "
                        f"folder={saved['event_folder']} | "
                        f"storage={saved['storage_status']}"
                    )

                    label += " | FALL ALERT"

                cv2.putText(
                    annotated,
                    label,
                    (max(5, x1), max(22, y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.65,
                    (0, 0, 255)
                    if track_id in self.active_falls
                    else (0, 220, 0),
                    2,
                    cv2.LINE_AA,
                )

        cv2.putText(
            annotated,
            f"VISIONX FALL | frame {frame_index} "
            f"| session events {self.event_count}",
            (12, 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )

        return annotated, new_events

    def close(self):
        """Close session resources safely."""
        if self.closed:
            return

        self.closed = True

        if self.log_file is not None:
            try:
                self.log_file.close()
            except OSError as exc:
                print(f"WARNING: Error closing CSV log: {exc}")

        print(f"Fall event log: {self.event_log_path}")
        print(f"Event folder: {self.event_root}")
        print(f"Session output folder: {self.output_dir}")