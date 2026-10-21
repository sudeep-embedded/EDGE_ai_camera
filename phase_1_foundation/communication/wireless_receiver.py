
"""VISIONX unified entry point: video testing and ESP32-S3 live monitoring."""

from __future__ import annotations

import queue
import socket
import struct
import threading
import time
import tkinter as tk
from tkinter import filedialog
from pathlib import Path
import sys

import cv2
import numpy as np


HERE = Path(__file__).resolve()
PROJECT_ROOT = HERE.parents[2]
AI_DIR = PROJECT_ROOT / "phase_1_foundation" / "ai"

if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from fall_detection_core import FallDetector  # noqa: E402


# Existing ESP32-S3 UDP protocol: unchanged
MAGIC = b"OV56UDP1"
HEADER_FORMAT = "<8sIHHHHIH"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

UDP_PORT = 5500
UDP_PACKET_SIZE = 1400
MAX_CHUNK_DATA = UDP_PACKET_SIZE - HEADER_SIZE
MAX_JPEG_SIZE = 256 * 1024
FRAME_TIMEOUT = 0.35
MAX_ACTIVE_FRAMES = 4
SOCKET_BUFFER = 4 * 1024 * 1024

# The receiver retains the latest completed frame for AI processing.
latest_jpeg = queue.Queue(maxsize=1)
stop_event = threading.Event()


def _read_menu_choice(prompt: str, options: dict[str, str]) -> str:
    while True:
        print(prompt)
        for key, label in options.items():
            print(f"  {key}. {label}")

        choice = input("Enter choice: ").strip()
        if choice in options:
            return choice

        print(f"Please enter one of: {', '.join(options.keys())}\n")


def choose_mode():
    print("\n" + "=" * 54)
    print("             VISIONX - INPUT MODE")
    print("=" * 54)

    choice = _read_menu_choice(
        "Choose how VISIONX receives video:",
        {
            "1": "Video Testing (select an MP4 file)",
            "2": "Real-Time Monitoring (ESP32-S3 camera over Wi-Fi)",
            "0": "Exit",
        },
    )
    return {"1": "video", "2": "live", "0": None}[choice]


def choose_domain():
    print("\n" + "=" * 54)
    print("          VISIONX - APPLICATION FIELD")
    print("=" * 54)

    choice = _read_menu_choice(
        "Choose the field to work on:",
        {
            "1": "Classroom Intelligence",
            "2": "Patient Monitoring & Safety (Hospital)",
            "0": "Exit",
        },
    )
    return {"1": "classroom", "2": "patient", "0": None}[choice]


def domain_label(domain: str) -> str:
    return {
        "classroom": "CLASSROOM INTELLIGENCE",
        "patient": "PATIENT MONITORING & SAFETY",
    }[domain]


def choose_video():
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    try:
        path = filedialog.askopenfilename(
            title="Select VISIONX test video",
            filetypes=[
                ("Video files", "*.mp4 *.avi *.mov *.mkv *.wmv"),
                ("All files", "*.*"),
            ],
            parent=root,
        )
    finally:
        root.destroy()

    return path


def video_mode(detector, domain):
    """Run video testing and save an annotated test recording."""
    video_path = choose_video()
    if not video_path:
        print("No video selected.")
        return

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"OpenCV could not open video: {video_path}")

    fps = float(cap.get(cv2.CAP_PROP_FPS))
    if not np.isfinite(fps) or fps < 1 or fps > 240:
        fps = 25.0

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if width <= 0 or height <= 0:
        cap.release()
        raise RuntimeError("Video has invalid dimensions.")

    output_path = (
        detector.output_dir / f"annotated_{Path(video_path).stem}.mp4"
    )
    writer = cv2.VideoWriter(
        str(output_path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )

    if not writer.isOpened():
        cap.release()
        raise RuntimeError("Could not create annotated MP4 output.")

    window = "VISIONX - Video Testing"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)

    frame_index = 0
    loop_count = 0
    started = time.perf_counter()

    print(f"FIELD       : {domain_label(domain)}")
    print(f"VIDEO       : {video_path}")
    print(f"ANNOTATED   : {output_path}")
    print(f"EVIDENCE    : {detector.event_root}")
    print("Video loops continuously. Press Q or Esc to stop.")

    try:
        while True:
            ok, frame = cap.read()

            if not ok:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                detector.reset_temporal_state()
                frame_index = 0
                loop_count += 1
                print(f"Video replay: loop {loop_count + 1}")
                continue

            frame_index += 1
            video_time = frame_index / fps

            annotated, events = detector.process_frame(
                frame,
                source="video",
                time_seconds=video_time,
                frame_index=frame_index,
            )

            cv2.putText(
                annotated,
                f"FIELD: {domain_label(domain)}",
                (12, 78),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )

            writer.write(annotated)

            scale = min(1100 / width, 700 / height, 1.0)
            if scale < 1:
                display = cv2.resize(
                    annotated,
                    (int(width * scale), int(height * scale)),
                    interpolation=cv2.INTER_AREA,
                )
            else:
                display = annotated

            cv2.imshow(window, display)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break

    finally:
        cap.release()
        writer.release()
        cv2.destroyAllWindows()

    print(f"Annotated video: {output_path}")
    print(f"Elapsed: {time.perf_counter() - started:.1f}s")


def publish_latest(jpeg_data: bytes, sequence: int):
    """Keep the latest completed frame for AI processing."""
    item = (jpeg_data, sequence, time.perf_counter())

    try:
        latest_jpeg.put_nowait(item)
        return
    except queue.Full:
        pass

    try:
        latest_jpeg.get_nowait()
    except queue.Empty:
        pass

    try:
        latest_jpeg.put_nowait(item)
    except queue.Full:
        pass


def receive_loop(sock: socket.socket):
    """Reassemble chunked UDP JPEG frames from the ESP32-S3."""
    frames = {}

    while not stop_event.is_set():
        now = time.perf_counter()

        expired = [
            seq for seq, state in frames.items()
            if now - state["last_time"] > FRAME_TIMEOUT
        ]
        for seq in expired:
            del frames[seq]

        try:
            packet, _address = sock.recvfrom(UDP_PACKET_SIZE)
        except socket.timeout:
            continue
        except OSError:
            if stop_event.is_set():
                break
            continue

        if len(packet) < HEADER_SIZE or len(packet) > UDP_PACKET_SIZE:
            continue

        try:
            (
                magic,
                sequence,
                width,
                height,
                chunk_index,
                total_chunks,
                jpeg_length,
                chunk_length,
            ) = struct.unpack(HEADER_FORMAT, packet[:HEADER_SIZE])
        except struct.error:
            continue

        payload = packet[HEADER_SIZE:]

        if (
            magic != MAGIC
            or width != 640
            or height != 480
            or total_chunks == 0
            or chunk_index >= total_chunks
            or jpeg_length < 4
            or jpeg_length > MAX_JPEG_SIZE
            or chunk_length != len(payload)
            or chunk_length > MAX_CHUNK_DATA
        ):
            continue

        expected_chunks = (
            jpeg_length + MAX_CHUNK_DATA - 1
        ) // MAX_CHUNK_DATA
        if total_chunks != expected_chunks:
            continue

        expected_chunk_size = (
            MAX_CHUNK_DATA
            if chunk_index < total_chunks - 1
            else jpeg_length - MAX_CHUNK_DATA * (total_chunks - 1)
        )
        if chunk_length != expected_chunk_size:
            continue

        state = frames.get(sequence)
        if state is None:
            if len(frames) >= MAX_ACTIVE_FRAMES:
                oldest = min(
                    frames,
                    key=lambda seq: frames[seq]["first_time"],
                )
                del frames[oldest]

            state = {
                "jpeg_length": jpeg_length,
                "total_chunks": total_chunks,
                "chunks": {},
                "first_time": now,
                "last_time": now,
            }
            frames[sequence] = state
        elif (
            state["jpeg_length"] != jpeg_length
            or state["total_chunks"] != total_chunks
        ):
            del frames[sequence]
            continue

        state["chunks"].setdefault(chunk_index, payload)
        state["last_time"] = now

        if len(state["chunks"]) != total_chunks:
            continue

        jpeg_data = b"".join(
            state["chunks"][index] for index in range(total_chunks)
        )
        del frames[sequence]

        if (
            len(jpeg_data) != jpeg_length
            or jpeg_data[:2] != b"\xff\xd8"
            or jpeg_data[-2:] != b"\xff\xd9"
        ):
            continue

        publish_latest(jpeg_data, sequence)


def live_mode(detector, domain):
    """Run live fall detection and continuously save annotated MP4 video."""
    # Remove any stale queued frame from an earlier run.
    while True:
        try:
            latest_jpeg.get_nowait()
        except queue.Empty:
            break

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(
            socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER
        )
        sock.settimeout(0.2)
        sock.bind(("0.0.0.0", UDP_PORT))
    except OSError:
        sock.close()
        raise

    stop_event.clear()
    thread = threading.Thread(
        target=receive_loop,
        args=(sock,),
        daemon=True,
        name="VISIONX-UDP-Receiver",
    )
    thread.start()

    window = "VISIONX - Live Fall Monitoring"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)

    frame_index = 0
    last_sequence = -1
    started = time.perf_counter()

    # Continuous video goes into this run's session directory.
    recording_path = detector.output_dir / "live_recording.mp4"
    video_writer = None
    recording_fps = 10.0
    recording_size = None
    recording_failed = False

    print(f"FIELD       : {domain_label(domain)}")
    print(f"REAL-TIME   : UDP port {UDP_PORT}")
    print(f"AI MODEL    : {detector.model_path}")
    print(f"EVIDENCE    : {detector.event_root}")
    print(f"RECORDING   : {recording_path}")
    print("Recording starts with the first valid camera frame.")
    print("Press Q or Esc to stop and finalize the MP4.")

    try:
        while True:
            item = None

            # Keep the existing low-latency latest-frame AI behavior.
            while True:
                try:
                    item = latest_jpeg.get_nowait()
                except queue.Empty:
                    break

            if item is not None:
                jpeg_data, sequence, completed_at = item
                frame = cv2.imdecode(
                    np.frombuffer(jpeg_data, dtype=np.uint8),
                    cv2.IMREAD_COLOR,
                )

                if frame is not None:
                    frame_index += 1
                    last_sequence = sequence

                    annotated, events = detector.process_frame(
                        frame,
                        source="live",
                        time_seconds=time.perf_counter() - started,
                        frame_index=frame_index,
                    )

                    cv2.putText(
                        annotated,
                        f"FIELD: {domain_label(domain)}",
                        (12, 78),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.55,
                        (255, 255, 255),
                        2,
                        cv2.LINE_AA,
                    )

                    age_ms = (time.perf_counter() - completed_at) * 1000
                    cv2.putText(
                        annotated,
                        f"UDP seq {sequence} | age {age_ms:.0f} ms",
                        (12, 54),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.55,
                        (255, 255, 255),
                        2,
                        cv2.LINE_AA,
                    )

                    # Open the writer only after frame dimensions are known.
                    # If recording fails, keep fall detection running.
                    if video_writer is None and not recording_failed:
                        height, width = annotated.shape[:2]
                        recording_size = (width, height)

                        candidate_writer = cv2.VideoWriter(
                            str(recording_path),
                            cv2.VideoWriter_fourcc(*"mp4v"),
                            recording_fps,
                            recording_size,
                        )

                        if candidate_writer.isOpened():
                            video_writer = candidate_writer
                            print(
                                "CONTINUOUS RECORDING STARTED: "
                                f"{recording_path} "
                                f"({width}x{height} at "
                                f"{recording_fps:.1f} FPS)"
                            )
                        else:
                            candidate_writer.release()
                            recording_failed = True
                            print(
                                "WARNING: Could not create live_recording.mp4. "
                                "Fall detection will continue without recording."
                            )

                    if video_writer is not None:
                        try:
                            frame_to_write = annotated
                            if (
                                frame_to_write.shape[1] != recording_size[0]
                                or frame_to_write.shape[0] != recording_size[1]
                            ):
                                frame_to_write = cv2.resize(
                                    frame_to_write, recording_size
                                )
                            video_writer.write(frame_to_write)
                        except Exception as exc:
                            print(
                                "WARNING: Live video write failed; "
                                f"AI will continue: {exc}"
                            )
                            video_writer.release()
                            video_writer = None
                            recording_failed = True

                    cv2.imshow(window, annotated)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break

    finally:
        stop_event.set()
        sock.close()
        thread.join(timeout=1.0)

        # Releasing the writer finalizes the MP4 container.
        if video_writer is not None:
            video_writer.release()
            print(f"CONTINUOUS RECORDING SAVED: {recording_path}")

        cv2.destroyAllWindows()

    print(
        f"Live frames processed: {frame_index}; "
        f"last UDP sequence: {last_sequence}"
    )
    if recording_failed:
        print("WARNING: A complete live MP4 was not saved.")


def main():
    mode = choose_mode()
    if mode is None:
        print("Exiting VISIONX.")
        return 0

    domain = choose_domain()
    if domain is None:
        print("Exiting VISIONX.")
        return 0

    storage_mode = {
        "video": "video_testing",
        "live": "real_time",
    }[mode]

    print("\n" + "=" * 54)
    print("VISIONX CONFIGURATION")
    print(
        f"Input mode : "
        f"{'VIDEO TESTING' if mode == 'video' else 'REAL-TIME ESP32-S3'}"
    )
    print(f"Storage    : recording/{storage_mode}/{domain}/")
    print(f"Field      : {domain_label(domain)}")
    print("Current AI : Fall detection only")
    print("=" * 54 + "\n")

    detector = FallDetector(
        domain=domain,
        input_mode=storage_mode,
    )

    try:
        if mode == "video":
            video_mode(detector, domain)
        else:
            live_mode(detector, domain)
    finally:
        detector.close()

    print("Note: SMS alerts are not wired into this version yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
