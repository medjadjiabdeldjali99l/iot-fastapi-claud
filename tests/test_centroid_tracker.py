"""Tests unitaires du centroid tracker OpenCV (_CentroidTracker).
Vérifie l'attribution/continuité des IDs, la création de nouveaux IDs hors
distance, et l'oubli après disparition prolongée."""
from app.services.detection_opencv import _CentroidTracker


def _box(cx, cy, size=10):
    return (cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2)


def test_registers_new_objects_in_order():
    t = _CentroidTracker()
    ids = t.update([_box(10, 10), _box(100, 100)])
    assert ids == [0, 1]


def test_tracks_same_object_across_small_movement():
    t = _CentroidTracker(max_distance=80.0)
    assert t.update([_box(10, 10)]) == [0]
    # léger déplacement -> même id
    assert t.update([_box(15, 12)]) == [0]


def test_new_id_when_jump_exceeds_max_distance():
    t = _CentroidTracker(max_distance=50.0)
    assert t.update([_box(10, 10)]) == [0]
    # saut > max_distance -> ancien objet non apparié, nouvel id
    assert t.update([_box(500, 500)]) == [1]


def test_object_forgotten_after_max_disappeared():
    t = _CentroidTracker(max_disappeared=2, max_distance=80.0)
    assert t.update([_box(10, 10)]) == [0]
    # 3 frames sans détection (> max_disappeared) -> objet 0 oublié
    t.update([])
    t.update([])
    t.update([])
    # réapparition au même endroit -> nouvel id (preuve de l'oubli)
    assert t.update([_box(10, 10)]) == [1]


def test_object_survives_short_disappearance():
    t = _CentroidTracker(max_disappeared=5, max_distance=80.0)
    assert t.update([_box(10, 10)]) == [0]
    t.update([])  # 1 frame d'absence, < max_disappeared
    # réapparition proche -> conserve l'id 0
    assert t.update([_box(12, 11)]) == [0]


def test_empty_update_returns_empty_list():
    t = _CentroidTracker()
    assert t.update([]) == []
