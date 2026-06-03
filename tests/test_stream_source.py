"""Tests unitaires de la détection de type de source (`_is_file_source`).
Cas limites : webcam (chiffre), flux réseau, chemins POSIX/Windows, file://."""
import pytest

from app.services.stream import _is_file_source


@pytest.mark.parametrize("url", ["0", "1", "10"])
def test_webcam_index_is_not_a_file(url):
    assert _is_file_source(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "rtsp://192.168.1.10:554/stream",
        "http://cam.local/video",
        "https://cam.local/video",
        "rtmp://server/live",
    ],
)
def test_network_streams_are_not_files(url):
    assert _is_file_source(url) is False


def test_posix_path_is_a_file():
    assert _is_file_source("/home/pi/videos/test.mp4") is True


def test_bare_filename_is_a_file():
    assert _is_file_source("test.mp4") is True


def test_windows_path_is_a_file():
    # urlparse voit "c" comme scheme d'une lettre -> traité comme fichier
    assert _is_file_source("C:\\videos\\test.mp4") is True


def test_file_uri_is_a_file():
    assert _is_file_source("file:///home/pi/test.mp4") is True
