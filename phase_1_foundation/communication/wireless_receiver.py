"""
VISIONX low-latency UDP JPEG receiver.

Compatible with ESP32-S3 firmware using:
    Magic: OV56UDP1
    Header: 26 bytes
    Datagram size: maximum 1400 bytes
    UDP port: 5500

Reception/reassembly and display decoding run separately.
Only the newest complete JPEG is retained for display.
"""

import collections
import queue
import socket
import struct
import threading
import time

import cv2
import numpy as np


# ---------------------------------------------------------
# Protocol configuration -- must match main.c
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# Latest-frame queue and statistics
# ---------------------------------------------------------

latest_jpeg = queue.Queue(maxsize=1)
stop_event = threading.Event()
stats_lock = threading.Lock()

stats = {
    "packets": 0,
    "bad_packets": 0,
    "completed": 0,
    "incomplete": 0,
    "queue_replaced": 0,
    "last_sequence": None,
}


def increment_stat(name, amount=1):
    with stats_lock:
        stats[name] += amount


def publish_latest(jpeg_data, sequence):
    """Keep the newest complete JPEG; never queue stale images."""
    item = (jpeg_data, sequence, time.perf_counter())

    try:
        latest_jpeg.put_nowait(item)
        return
    except queue.Full:
        pass

    try:
        latest_jpeg.get_nowait()
        increment_stat("queue_replaced")
    except queue.Empty:
        pass

    try:
        latest_jpeg.put_nowait(item)
    except queue.Full:
        # Another update won the race; dropping is safe.
        pass


# ---------------------------------------------------------
# Packet receive and frame reassembly thread
# ---------------------------------------------------------

def receive_loop(sock):
    frames = {}

    while not stop_event.is_set():
        now = time.perf_counter()

        # Expire incomplete frames.
        expired = [
            seq
            for seq, state in frames.items()
            if now - state["last_time"] > FRAME_TIMEOUT
        ]

        for seq in expired:
            del frames[seq]
            increment_stat("incomplete")

        try:
            # Buffer is large enough for a complete UDP datagram.
            packet, _address = sock.recvfrom(UDP_PACKET_SIZE)

        except socket.timeout:
            continue

        except OSError:
            if stop_event.is_set():
                break
            continue

        increment_stat("packets")

        if len(packet) < HEADER_SIZE or len(packet) > UDP_PACKET_SIZE:
            increment_stat("bad_packets")
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
            increment_stat("bad_packets")
            continue

        payload = packet[HEADER_SIZE:]

        # Validate protocol and dimensions.
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
            increment_stat("bad_packets")
            continue

        # Ensure chunk count matches the announced JPEG size.
        expected_chunks = (
            jpeg_length + MAX_CHUNK_DATA - 1
        ) // MAX_CHUNK_DATA

        if total_chunks != expected_chunks:
            increment_stat("bad_packets")
            continue

        # Verify the expected size of this individual chunk.
        expected_chunk_size = (
            MAX_CHUNK_DATA
            if chunk_index < total_chunks - 1
            else jpeg_length - MAX_CHUNK_DATA * (total_chunks - 1)
        )

        if chunk_length != expected_chunk_size:
            increment_stat("bad_packets")
            continue

        state = frames.get(sequence)

        if state is None:
            # Bound memory usage if multiple frames are incomplete.
            if len(frames) >= MAX_ACTIVE_FRAMES:
                oldest_seq = min(
                    frames,
                    key=lambda seq: frames[seq]["first_time"],
                )
                del frames[oldest_seq]
                increment_stat("incomplete")

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
            increment_stat("bad_packets")
            continue

        # Ignore duplicate chunks.
        state["chunks"].setdefault(chunk_index, payload)
        state["last_time"] = now

        if len(state["chunks"]) != total_chunks:
            continue

        # Reassemble in chunk order.
        try:
            jpeg_data = b"".join(
                state["chunks"][index]
                for index in range(total_chunks)
            )
        except KeyError:
            continue

        del frames[sequence]

        # Reject truncated or invalid JPEG frames.
        if (
            len(jpeg_data) != jpeg_length
            or jpeg_data[:2] != b"\xff\xd8"
            or jpeg_data[-2:] != b"\xff\xd9"
        ):
            increment_stat("incomplete")
            continue

        increment_stat("completed")

        with stats_lock:
            stats["last_sequence"] = sequence

        publish_latest(jpeg_data, sequence)


# ---------------------------------------------------------
# Main display loop
# ---------------------------------------------------------

def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        sock.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_RCVBUF,
            SOCKET_BUFFER,
        )

        # A short timeout allows clean shutdown.
        sock.settimeout(0.2)
        sock.bind(("0.0.0.0", UDP_PORT))

    except OSError:
        sock.close()
        raise

    receiver_thread = threading.Thread(
        target=receive_loop,
        args=(sock,),
        daemon=True,
        name="VISIONX-UDP-Receiver",
    )
    receiver_thread.start()

    print("=" * 70)
    print("VISIONX - LOW-LATENCY UDP JPEG RECEIVER")
    print(f"UDP port       : {UDP_PORT}")
    print(f"Header size    : {HEADER_SIZE} bytes")
    print(f"Max datagram   : {UDP_PACKET_SIZE} bytes")
    print("Receive thread : enabled")
    print("Display policy : newest complete frame")
    print("Press Q or ESC to quit.")
    print("=" * 70)

    display_fps = 0
    last_report = time.perf_counter()

    last_sequence = -1
    last_jpeg_kb = 0.0
    latest_age_ms = 0.0

    try:
        while True:
            # Drain any stale completed images before decoding.
            item = None

            while True:
                try:
                    item = latest_jpeg.get_nowait()
                except queue.Empty:
                    break

            if item is not None:
                jpeg_data, sequence, completed_at = item

                encoded = np.frombuffer(
                    jpeg_data,
                    dtype=np.uint8,
                )

                image = cv2.imdecode(
                    encoded,
                    cv2.IMREAD_COLOR,
                )

                if image is not None:
                    cv2.imshow(
                        "VISIONX - ESP32-S3 OV5640",
                        image,
                    )

                    display_fps += 1
                    last_sequence = sequence
                    last_jpeg_kb = len(jpeg_data) / 1024.0

                    # Time since reassembly completed, not sensor capture.
                    latest_age_ms = (
                        time.perf_counter() - completed_at
                    ) * 1000.0

            key = cv2.waitKey(1) & 0xFF

            if key in (ord("q"), 27):
                break

            now = time.perf_counter()

            if now - last_report >= 1.0:
                elapsed = now - last_report
                fps = display_fps / elapsed

                with stats_lock:
                    snapshot = dict(stats)

                    # Reset interval counters only.
                    stats["packets"] = 0
                    stats["bad_packets"] = 0
                    stats["completed"] = 0
                    stats["incomplete"] = 0
                    stats["queue_replaced"] = 0

                print(
                    f"\rFPS={fps:4.1f}"
                    f" | seq={last_sequence:8d}"
                    f" | JPEG={last_jpeg_kb:5.1f}KB"
                    f" | age~{latest_age_ms:6.1f}ms"
                    f" | packets={snapshot['packets']:5d}"
                    f" | complete={snapshot['completed']:3d}"
                    f" | incomplete={snapshot['incomplete']:3d}"
                    f" | bad={snapshot['bad_packets']:3d}"
                    f" | stale_drop={snapshot['queue_replaced']:3d}",
                    end="",
                    flush=True,
                )

                display_fps = 0
                last_report = now

    finally:
        stop_event.set()
        sock.close()
        receiver_thread.join(timeout=1.0)
        cv2.destroyAllWindows()
        print("\nReceiver stopped.")


if __name__ == "__main__":
    main()