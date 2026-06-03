"""Tests unitaires de LineCrosser (franchissement de ligne).
Cas nominaux, anti-double-comptage, filtrage de direction, validation."""
import pytest

from app.services.detection import Detection
from app.services.tracking import LineCrosser


def _det(tracker_id, cx):
    """Détection dont le centre horizontal vaut `cx` (boîte de 20px de large)."""
    return Detection(
        tracker_id=tracker_id,
        bbox=(cx - 10.0, 0.0, cx + 10.0, 10.0),
        confidence=1.0,
        class_id=0,
        class_name="object",
    )


FRAME_W = 100  # ligne à 0.5 => line_x = 50


def test_validation_rejects_out_of_range_position():
    with pytest.raises(ValueError):
        LineCrosser(line_position=1.5)
    with pytest.raises(ValueError):
        LineCrosser(line_position=-0.1)


def test_no_event_on_first_frame():
    lc = LineCrosser(line_position=0.5)
    # première vue d'un id : on enregistre sa position, pas d'event
    assert lc.update([_det(1, 40)], FRAME_W) == []


def test_crossing_left_to_right_emits_one_event():
    lc = LineCrosser(line_position=0.5)
    lc.update([_det(1, 40)], FRAME_W)            # avant la ligne
    events = lc.update([_det(1, 60)], FRAME_W)   # après la ligne
    assert len(events) == 1
    assert events[0].tracker_id == 1
    assert events[0].direction == "left_to_right"


def test_same_id_counted_once():
    # RÉGRESSION : un même objet ne doit être compté qu'une fois par itération
    lc = LineCrosser(line_position=0.5)
    lc.update([_det(1, 40)], FRAME_W)
    lc.update([_det(1, 60)], FRAME_W)            # 1er franchissement
    again = lc.update([_det(1, 80)], FRAME_W)    # encore après -> ignoré
    assert again == []
    assert lc.crossed_ids == {1}


def test_direction_filter_blocks_opposite():
    lc = LineCrosser(line_position=0.5, direction="right_to_left")
    lc.update([_det(1, 40)], FRAME_W)
    events = lc.update([_det(1, 60)], FRAME_W)   # va gauche->droite, filtré
    assert events == []


def test_detection_without_tracker_id_ignored():
    lc = LineCrosser(line_position=0.5)
    lc.update([_det(None, 40)], FRAME_W)
    events = lc.update([_det(None, 60)], FRAME_W)
    assert events == []


def test_two_objects_cross_independently():
    lc = LineCrosser(line_position=0.5)
    lc.update([_det(1, 40), _det(2, 30)], FRAME_W)
    events = lc.update([_det(1, 60), _det(2, 70)], FRAME_W)
    assert {e.tracker_id for e in events} == {1, 2}


def test_reset_clears_state():
    lc = LineCrosser(line_position=0.5)
    lc.update([_det(1, 40)], FRAME_W)
    lc.update([_det(1, 60)], FRAME_W)
    lc.reset()
    assert lc.crossed_ids == set()
    # après reset, le même id peut de nouveau être compté
    lc.update([_det(1, 40)], FRAME_W)
    assert len(lc.update([_det(1, 60)], FRAME_W)) == 1
