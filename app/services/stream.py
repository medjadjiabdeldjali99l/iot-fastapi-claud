import asyncio
import os
from typing import AsyncIterator
from urllib.parse import urlparse

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


def _is_file_source(url: str) -> bool:
    """`stream_url` est-il un chemin de fichier vidéo plutôt qu'un flux ?

    - "0", "1", … → webcam locale (faux)
    - schéma http/https/rtsp → flux réseau (faux)
    - schéma file:// → fichier (vrai)
    - sinon (chemin nu Windows ou POSIX) → fichier (vrai)
    """
    if url.isdigit():
        return False
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme in ("http", "https", "rtsp", "rtmp"):
        return False
    if scheme == "file":
        return True
    # Pas de schéma reconnu : on traite comme un chemin local.
    # Cas Windows : "C:\\path\\foo.mp4" -> urlparse renvoie scheme="c" (sic).
    # On le tolère explicitement.
    if len(scheme) == 1 and scheme.isalpha():
        return True
    return scheme == ""


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
        # Si la source est un fichier vidéo, on rejoue en boucle plutôt que
        # de lever `StreamUnavailable` à la fin du fichier (= simulation
        # d'un flux continu pour tester sur Pi/PC sans caméra réelle).
        self._is_file = _is_file_source(url)

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

        # Fichier vidéo local : on laisse OpenCV choisir le backend (FFmpeg
        # par défaut). Pas de timeouts réseau utiles ici.
        if self._is_file:
            # `file://` est valide pour OpenCV mais on retire le préfixe au
            # cas où, pour la portabilité Windows.
            path = self.url[len("file://"):] if self.url.startswith("file://") else self.url
            cap = cv2.VideoCapture(path)
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
        down the FFmpeg session.

        - Caméra / flux réseau : `StreamUnavailable` à la perte de signal.
        - Fichier vidéo : rejoue en boucle indéfiniment (`seek 0` à l'EOF)
          pour simuler un flux continu pendant les tests."""
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
                    if self._is_file:
                        # EOF : on rembobine et on continue.
                        try:
                            await asyncio.to_thread(
                                cap.set, cv2.CAP_PROP_POS_FRAMES, 0
                            )
                        except cv2.error:
                            pass
                        ret, frame = await asyncio.to_thread(cap.read)
                        if not ret or frame is None:
                            # Fichier illisible / vide après seek → on stop.
                            raise StreamUnavailable(
                                f"Cannot loop file: {self.url}"
                            )
                    else:
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
