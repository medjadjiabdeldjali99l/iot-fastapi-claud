"""Tests d'intégration de l'API (FastAPI TestClient).

- /health : sans auth.
- endpoints protégés : 401 sans token.
- GET /sessions : via override de la dépendance d'auth + client Supabase factice
  (pas de vraie connexion réseau/DB).
"""
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.db.deps import Auth, get_auth
from app.main import app

client = TestClient(app)


# --- /health --------------------------------------------------------------

def test_health_ok():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "app" in body and "environment" in body


# --- auth requise ---------------------------------------------------------

def test_sessions_requires_auth():
    # pas de header Authorization -> 401 (avant tout accès DB)
    resp = client.get("/sessions")
    assert resp.status_code == 401


def test_invalid_bearer_rejected():
    resp = client.get("/sessions", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


# --- GET /sessions avec auth overridée + DB factice -----------------------

class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """Imite le query builder Supabase : tous les filtres renvoient self."""
    def __init__(self, data):
        self._data = data

    def select(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return _FakeResult(self._data)


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows

    def table(self, _name):
        return _FakeQuery(self._rows)


def _summary_row(session_id, org_id, cam_id):
    return {
        "session_id": str(session_id),
        "organization_id": str(org_id),
        "camera_id": str(cam_id),
        "status": "stopped",
        "interval_minutes": 5,
        "measurement_window_seconds": 120,
        "reference_cadence_min": 20.0,
        "reference_cadence_max": 30.0,
        "started_at": "2026-06-01T10:00:00+00:00",
        "ended_at": "2026-06-01T10:30:00+00:00",
        "iteration_count": 3,
        "avg_opm": 24.5,
        "min_opm": 20.1,
        "max_opm": 28.0,
        "anomaly_count": 1,
        "below_count": 0,
        "normal_count": 2,
        "above_count": 1,
    }


def test_list_sessions_with_override(fake_user_id):
    sid, org, cam = uuid4(), uuid4(), uuid4()
    rows = [_summary_row(sid, org, cam)]

    app.dependency_overrides[get_auth] = lambda: Auth(
        user_id=fake_user_id, db=_FakeClient(rows)
    )
    try:
        resp = client.get(
            "/sessions", headers={"Authorization": "Bearer overridden"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["session_id"] == str(sid)
        assert data[0]["avg_opm"] == 24.5
        assert data[0]["normal_count"] == 2
    finally:
        app.dependency_overrides.clear()


def test_list_sessions_empty(fake_user_id):
    app.dependency_overrides[get_auth] = lambda: Auth(
        user_id=fake_user_id, db=_FakeClient([])
    )
    try:
        resp = client.get(
            "/sessions", headers={"Authorization": "Bearer overridden"}
        )
        assert resp.status_code == 200
        assert resp.json() == []
    finally:
        app.dependency_overrides.clear()
