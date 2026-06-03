"""Tests unitaires des helpers purs du runner de cadence + la fabrique de
détecteur. Couvre cas nominaux, cas limites et régressions documentées
(OPM NULL si < 2 objets, statut NULL sans plage de référence)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.models.common import CadenceStatus
from app.services.cadence import (
    compute_anomaly,
    compute_avg_delta_seconds,
    compute_cadence_status,
    make_detector,
)
from app.services.detection import YoloDetector
from app.services.detection_opencv import OpenCvDetector


def _ts(*offsets_s):
    """Construit une liste de datetimes à partir d'offsets en secondes."""
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return [base + timedelta(seconds=o) for o in offsets_s]


# --- compute_avg_delta_seconds -------------------------------------------

def test_avg_delta_nominal():
    # écarts 2s, 4s -> moyenne 3s
    assert compute_avg_delta_seconds(_ts(0, 2, 6)) == pytest.approx(3.0)


def test_avg_delta_uniform():
    # écarts constants de 5s
    assert compute_avg_delta_seconds(_ts(0, 5, 10, 15)) == pytest.approx(5.0)


def test_avg_delta_zero_timestamps_returns_none():
    assert compute_avg_delta_seconds([]) is None


def test_avg_delta_single_timestamp_returns_none():
    # RÉGRESSION : < 2 franchissements => pas d'écart => None (OPM NULL en aval)
    assert compute_avg_delta_seconds(_ts(0)) is None


# --- compute_anomaly ------------------------------------------------------

def test_anomaly_no_baseline_returns_none():
    assert compute_anomaly(25.0, [], 15.0) is None


def test_anomaly_baseline_non_positive_returns_none():
    assert compute_anomaly(25.0, [0.0, 0.0], 15.0) is None


def test_anomaly_within_threshold():
    # moyenne 20, courant 22 -> écart 10% < 15% -> pas une anomalie
    is_anom, dev = compute_anomaly(22.0, [20.0, 20.0], 15.0)
    assert is_anom is False
    assert dev == pytest.approx(10.0)


def test_anomaly_beyond_threshold():
    # moyenne 20, courant 25 -> écart 25% > 15% -> anomalie
    is_anom, dev = compute_anomaly(25.0, [20.0, 20.0], 15.0)
    assert is_anom is True
    assert dev == pytest.approx(25.0)


def test_anomaly_below_baseline_uses_absolute_deviation():
    # courant sous la moyenne : l'écart est en valeur absolue
    is_anom, dev = compute_anomaly(15.0, [20.0], 15.0)
    assert is_anom is True
    assert dev == pytest.approx(25.0)


# --- compute_cadence_status ----------------------------------------------

def test_status_below():
    assert compute_cadence_status(10.0, 20.0, 30.0) is CadenceStatus.BELOW


def test_status_normal_inclusive_bounds():
    assert compute_cadence_status(20.0, 20.0, 30.0) is CadenceStatus.NORMAL
    assert compute_cadence_status(30.0, 20.0, 30.0) is CadenceStatus.NORMAL
    assert compute_cadence_status(25.0, 20.0, 30.0) is CadenceStatus.NORMAL


def test_status_above():
    assert compute_cadence_status(40.0, 20.0, 30.0) is CadenceStatus.ABOVE


def test_status_none_when_opm_missing():
    # RÉGRESSION : pas d'OPM (< 2 objets) => pas de comparaison
    assert compute_cadence_status(None, 20.0, 30.0) is None


def test_status_none_when_range_missing():
    # RÉGRESSION : pas de plage de référence => statut NULL
    assert compute_cadence_status(25.0, None, None) is None
    assert compute_cadence_status(25.0, 20.0, None) is None


# --- make_detector (fabrique selon DETECTOR_BACKEND) ---------------------

def test_make_detector_opencv(monkeypatch):
    monkeypatch.setattr(settings, "DETECTOR_BACKEND", "opencv")
    det = make_detector("yolov8n")
    assert isinstance(det, OpenCvDetector)


def test_make_detector_yolo(monkeypatch):
    monkeypatch.setattr(settings, "DETECTOR_BACKEND", "yolo")
    det = make_detector("yolov8n")
    assert isinstance(det, YoloDetector)


def test_make_detector_defaults_to_yolo_on_unknown(monkeypatch):
    # valeur inconnue / mal orthographiée => YOLO par sécurité
    monkeypatch.setattr(settings, "DETECTOR_BACKEND", "bidon")
    assert isinstance(make_detector("yolov8n"), YoloDetector)


def test_make_detector_case_insensitive(monkeypatch):
    monkeypatch.setattr(settings, "DETECTOR_BACKEND", "OpenCV")
    assert isinstance(make_detector("yolov8n"), OpenCvDetector)
