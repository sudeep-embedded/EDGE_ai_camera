"""Standalone VISIONX video test runner using the shared fall-detection core."""
from __future__ import annotations
import argparse
import time
import tkinter as tk
from tkinter import filedialog
from pathlib import Path
import cv2
import numpy as np
from fall_detection_core import FallDetector


def choose_video():
    root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
    path = filedialog.askopenfilename(title="Select VISIONX fall-detection test video", filetypes=[("Video files", "*.mp4 *.avi *.mov *.mkv *.wmv"), ("All files", "*.*")])
    root.destroy(); return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("video", nargs="?")
    parser.add_argument("--model", default=None)
    parser.add_argument("--confidence", type=float, default=0.35)
    args = parser.parse_args()
    video = args.video or choose_video()
    if not video:
        print("No video selected."); return 1
    cap = cv2.VideoCapture(str(Path(video).resolve()))
    if not cap.isOpened():
        print(f"Cannot open video: {video}"); return 2
    fps = cap.get(cv2.CAP_PROP_FPS)
    if not np.isfinite(fps) or fps < 1 or fps > 240: fps = 25.0
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    detector = FallDetector(model_path=args.model or __import__('fall_detection_core').DEFAULT_MODEL, confidence=args.confidence)
    writer = cv2.VideoWriter(str(detector.output_dir / f"annotated_{Path(video).stem}.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w,h))
    window = "VISIONX - Fall Detection Video Test"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    frame_index = 0; loop_count = 0; started = time.perf_counter()
    print("Continuous video test. Press Q or Esc to stop.")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0); detector.reset_temporal_state(); frame_index = 0; loop_count += 1
                print(f"Loop {loop_count + 1}"); continue
            frame_index += 1
            annotated, _ = detector.process_frame(frame, source="video", time_seconds=frame_index/fps, frame_index=frame_index)
            writer.write(annotated)
            scale = min(1100/w, 700/h, 1.0)
            shown = cv2.resize(annotated, (int(w*scale), int(h*scale)), interpolation=cv2.INTER_AREA) if scale < 1 else annotated
            cv2.imshow(window, shown)
            if cv2.waitKey(1) & 0xFF in (ord('q'), 27): break
    finally:
        cap.release(); writer.release(); cv2.destroyAllWindows(); detector.close()
    print(f"Elapsed: {time.perf_counter()-started:.1f}s")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
