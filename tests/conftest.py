"""Configuration pytest : rend le package `app` importable et fournit des
fixtures communes.

⚠️ Ces tests s'exécutent dans l'environnement backend complet (cv2, ultralytics,
numpy installés via requirements.txt). Lancer depuis `Backend/` :

    pip install -r requirements-dev.txt
    pytest -q
"""
import os
import sys
from uuid import UUID

import pytest

# Rendre `app` importable quel que soit le cwd (Backend/ sur le sys.path).
_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


@pytest.fixture
def fake_user_id() -> UUID:
    return UUID("2c9b1074-4223-471f-85a8-024d6749ab65")
