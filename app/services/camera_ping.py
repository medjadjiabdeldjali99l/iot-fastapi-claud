import asyncio
import os
from urllib.parse import urlparse

import httpx

from app.core.config import settings


async def ping_stream_url(url: str, timeout: float | None = None) -> bool:
    """Pings a camera source to check availability.

    - Digit string ('0', '1', …): local webcam — reported online, vraie
      vérification au moment d'ouvrir le flux.
    - http/https: HEAD avec fallback GET.
    - rtsp: TCP connect sur host:port (554 par défaut).
    - file:// ou chemin local : existence du fichier sur disque.
    """
    if url.isdigit():
        return True
    t = timeout if timeout is not None else settings.CAMERA_PING_TIMEOUT_SECONDS
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme in ("http", "https"):
        return await _ping_http(url, t)
    if scheme == "rtsp":
        return await _ping_rtsp(parsed.hostname, parsed.port or 554, t)
    if scheme == "file":
        return os.path.isfile(url[len("file://"):])
    # Aucun schéma reconnu (ou un seul caractère, ex. lettre de lecteur
    # Windows "C:") : on traite comme un chemin local.
    if scheme == "" or (len(scheme) == 1 and scheme.isalpha()):
        return os.path.isfile(url)
    return False


async def _ping_http(url: str, timeout: float) -> bool:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        try:
            resp = await client.head(url)
            if resp.status_code < 500:
                return True
        except httpx.RequestError:
            pass
        try:
            resp = await client.get(url)
            return resp.status_code < 500
        except httpx.RequestError:
            return False


async def _ping_rtsp(host: str | None, port: int, timeout: float) -> bool:
    if not host:
        return False
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (asyncio.TimeoutError, OSError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return True
