"""Tests d'intégration du détecteur OpenCV sur images synthétiques.
Exerce le vrai pipeline cv2 (MOG2 -> seuillage -> morphologie -> contours)
et l'attribution d'IDs. Pas de caméra ni de fichier réels."""
import asyncio

import numpy as np

from app.services.detection import Detection
from app.services.detection_opencv import OpenCvDetector

H, W = 240, 320


def _bg():
    """Fond gris uniforme."""
    return np.full((H, W, 3), 127, dtype=np.uint8)


def _bg_with_square(x, y, side=80, color=255):
    """Fond gris + un carré clair (objet) à (x, y)."""
    frame = _bg()
    frame[y:y + side, x:x + side] = color
    return frame


def _warm_up(det, frames=25):
    """Apprend le fond statique au MOG2 avant d'introduire un objet."""
    bg = _bg()
    for _ in range(frames):
        det._foreground_boxes(bg)


def test_foreground_detects_moving_object():
    det = OpenCvDetector(min_area=200)
    _warm_up(det)
    boxes = det._foreground_boxes(_bg_with_square(100, 80))
    assert len(boxes) >= 1
    # une boîte doit recouvrir grossièrement la zone du carré (100..180, 80..160)
    assert any(
        x1 < 180 and x2 > 100 and y1 < 160 and y2 > 80
        for (x1, y1, x2, y2) in boxes
    )


def test_static_background_yields_no_detection():
    det = OpenCvDetector(min_area=200)
    _warm_up(det)
    # même fond, aucun objet -> aucun contour significatif
    assert det._foreground_boxes(_bg()) == []


def test_min_area_filters_tiny_blobs():
    # un carré 10x10 (=100 px²) doit être rejeté si min_area=2000
    det = OpenCvDetector(min_area=2000)
    _warm_up(det)
    boxes = det._foreground_boxes(_bg_with_square(150, 120, side=10))
    assert boxes == []


def test_track_sync_assigns_tracker_ids():
    det = OpenCvDetector(min_area=200)
    _warm_up(det)
    dets = det.track_sync(_bg_with_square(100, 80))
    assert len(dets) >= 1
    d = dets[0]
    assert isinstance(d, Detection)
    assert isinstance(d.tracker_id, int)       # tracking actif
    assert d.class_name == "object"            # OpenCV ne nomme pas la classe
    assert d.confidence == 1.0                 # pas de score en vision classique


def test_track_keeps_id_across_frames():
    det = OpenCvDetector(min_area=200, max_distance=120.0)
    _warm_up(det)
    d1 = det.track_sync(_bg_with_square(100, 80))
    d2 = det.track_sync(_bg_with_square(108, 80))   # léger déplacement
    assert d1 and d2
    # l'id du blob principal doit survivre d'une frame à l'autre (tolérant à
    # d'éventuels contours fantômes générés par MOG2 lors du déplacement).
    assert d1[0].tracker_id in {d.tracker_id for d in d2}


def test_async_track_wrapper_runs():
    det = OpenCvDetector(min_area=200)
    _warm_up(det)
    dets = asyncio.run(det.track(_bg_with_square(100, 80)))
    assert isinstance(dets, list)
