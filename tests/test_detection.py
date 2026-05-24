"""CLI test for the detection + line-crossing pipeline.

Run from `backend/`:

    python -m tests.test_detection --source path/to/video.mp4
    python -m tests.test_detection --source 0                       # webcam
    python -m tests.test_detection --source video.mp4 --line 0.5 \\
        --model yolov8n --conf 0.5 --classes person --save out.mp4

Each detected line crossing is printed as a single line. Use --save to
write an annotated MP4 (line + boxes + tracker IDs + crossing circles)."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2

# Make `app.*` importable when running this file directly from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.detection import Detection, YoloDetector  # noqa: E402
from app.services.tracking import CrossingEvent, LineCrosser  # noqa: E402


def _parse_source(s: str):
    return int(s) if s.isdigit() else s


def _annotate(
    frame,
    detections: list[Detection],
    line_x: int,
    events: list[CrossingEvent],
):
    out = frame.copy()
    cv2.line(out, (line_x, 0), (line_x, out.shape[0]), (0, 255, 0), 2)
    for d in detections:
        x1, y1, x2, y2 = (int(v) for v in d.bbox)
        color = (255, 0, 0) if d.tracker_id is None else (0, 0, 255)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        tag = f"#{d.tracker_id}" if d.tracker_id is not None else "#?"
        label = f"{tag} {d.class_name} {d.confidence:.2f}"
        cv2.putText(
            out,
            label,
            (x1, max(15, y1 - 5)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            color,
            1,
        )
    for e in events:
        cx = int((e.detection.bbox[0] + e.detection.bbox[2]) / 2)
        cy = int((e.detection.bbox[1] + e.detection.bbox[3]) / 2)
        cv2.circle(out, (cx, cy), 24, (0, 255, 255), 3)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run YOLO detection + ByteTrack tracking + line-crossing "
            "detection on a video file or webcam."
        )
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Video file path, or webcam index as integer (e.g. '0')",
    )
    parser.add_argument("--model", default="yolov8n", help="YOLO model (default: yolov8n)")
    parser.add_argument(
        "--line",
        type=float,
        default=0.80,
        help="Vertical line position as ratio of width [0,1] (default: 0.80)",
    )
    parser.add_argument(
        "--conf",
        type=float,
        default=0.5,
        help="YOLO confidence threshold (default: 0.5)",
    )
    parser.add_argument(
        "--classes",
        nargs="*",
        help="Filter detections to these class names (default: all)",
    )
    parser.add_argument(
        "--direction",
        choices=["any", "left_to_right", "right_to_left"],
        default="any",
        help="Which crossing direction to count (default: any)",
    )
    parser.add_argument("--max-frames", type=int, help="Stop after N frames")
    parser.add_argument("--save", help="Write annotated MP4 to this path")
    args = parser.parse_args()

    source = _parse_source(args.source)
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print(f"ERROR: cannot open source: {args.source}", file=sys.stderr)
        return 1

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    line_x = int(width * args.line)

    print(f"Source     : {args.source} ({width}x{height}, {fps:.1f} fps)")
    print(f"Model      : {args.model}")
    print(f"Line       : {args.line:.2f} (x={line_x})")
    print(f"Confidence : {args.conf}")
    print(f"Direction  : {args.direction}")
    print(f"Classes    : {args.classes or 'all'}")
    print()

    detector = YoloDetector(args.model)
    crosser = LineCrosser(line_position=args.line, direction=args.direction)
    classes_filter = set(args.classes) if args.classes else None

    writer = None
    if args.save:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(args.save, fourcc, fps, (width, height))
        if not writer.isOpened():
            print(f"WARNING: cannot open writer for {args.save}", file=sys.stderr)
            writer = None

    frame_idx = 0
    total_crossings = 0
    t0 = time.monotonic()

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            frame_idx += 1
            if args.max_frames and frame_idx > args.max_frames:
                break

            detections = detector.track_sync(frame, confidence=args.conf)
            if classes_filter:
                detections = [d for d in detections if d.class_name in classes_filter]

            events = crosser.update(detections, width)

            for e in events:
                total_crossings += 1
                t_video = frame_idx / fps
                d = e.detection
                cx = int((d.bbox[0] + d.bbox[2]) / 2)
                print(
                    f"[{t_video:7.2f}s] frame {frame_idx:5d}  "
                    f"track#{d.tracker_id:<3}  "
                    f"{d.class_name:<14} "
                    f"conf={d.confidence:.2f}  "
                    f"dir={e.direction:<14}  "
                    f"x={cx}"
                )

            if writer is not None:
                writer.write(_annotate(frame, detections, line_x, events))
    except KeyboardInterrupt:
        print("\n(interrupted)")
    finally:
        cap.release()
        if writer is not None:
            writer.release()

    elapsed = time.monotonic() - t0
    fps_proc = frame_idx / elapsed if elapsed > 0 else 0.0
    print()
    print(f"Processed {frame_idx} frames in {elapsed:.1f}s ({fps_proc:.1f} fps).")
    print(f"Total crossings: {total_crossings}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
