import asyncio
import os
from typing import AsyncIterator

import numpy as np

# FFmpeg capture options must be set BEFORE importing cv2.
# - rtsp_transport;tcp : avoid UDP packet loss / NAT issues
# - stimeout (microseconds) : abort read after 5 s of silence so cap.read()
#   returns False on stalled streams and the generator can clean up.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|stimeout;5000000",
)

import cv2  # noqa: E402


class StreamError(Exception):
    pass


class StreamUnavailable(StreamError):
    pass


class RTSPStream:
    """Async wrapper around cv2.VideoCapture for RTSP/HTTP video sources.

    cv2 calls run in worker threads to keep the event loop free. The
    capture is always released in the `frames()` finally block, so closing
    the consumer (cancel / aclose / break) tears down the FFmpeg session."""

    def __init__(
        self,
        url: str,
        target_fps: int = 10,
        jpeg_quality: int = 70,
    ) -> None:
        self.url = url
        self.target_fps = max(1, min(target_fps, 30))
        self.jpeg_quality = max(10, min(jpeg_quality, 95))
        self._cap: cv2.VideoCapture | None = None

    def _open_capture(self) -> cv2.VideoCapture:
        # Dev shortcut: `stream_url = "0"` (or "1", …) opens the local webcam
        # via the platform default backend (DShow/MSMF on Windows, V4L2 on
        # Linux). Production uses RTSP/HTTP URLs through FFmpeg.
        if self.url.isdigit():
            cap = cv2.VideoCapture(int(self.url))
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except cv2.error:
                pass
            return cap

        # Set timeouts *before* opening so a bad URL fails in ~5 s instead of
        # the FFmpeg default of 30 s.
        cap = cv2.VideoCapture()
        for prop, value in (
            (cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000),
            (cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000),
            (cv2.CAP_PROP_BUFFERSIZE, 1),
        ):
            try:
                cap.set(prop, value)
            except cv2.error:
                pass
        cap.open(self.url, cv2.CAP_FFMPEG)
        return cap

    async def raw_frames(self) -> AsyncIterator[np.ndarray]:
        """Yield BGR numpy frames at target_fps. Releases the capture in
        `finally` so cancelling the consumer (cancel / aclose / break) tears
        down the FFmpeg session. Raises StreamUnavailable on open failure
        or stream interruption."""
        cap = await asyncio.to_thread(self._open_capture)
        if not cap.isOpened():
            await asyncio.to_thread(cap.release)
            raise StreamUnavailable(f"Cannot open stream: {self.url}")
        self._cap = cap

        interval = 1.0 / self.target_fps
        try:
            while True:
                ret, frame = await asyncio.to_thread(cap.read)
                if not ret or frame is None:
                    raise StreamUnavailable("Stream interrupted")
                yield frame
                await asyncio.sleep(interval)
        finally:
            self._cap = None
            await asyncio.to_thread(cap.release)

    async def frames(self) -> AsyncIterator[bytes]:
        """JPEG-encoded view of `raw_frames()` — used by the WebSocket preview."""
        async for frame in self.raw_frames():
            ok, jpeg = cv2.imencode(
                ".jpg",
                frame,
                [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality],
            )
            if ok:
                yield bytes(jpeg)

    async def close(self) -> None:
        """Idempotent release; safe to call if frames() never ran."""
        cap = self._cap
        if cap is None:
            return
        self._cap = None
        await asyncio.to_thread(cap.release)
