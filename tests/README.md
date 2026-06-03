# Tests — IoT Cadence Backend

> Étape 3/3 — Suite de tests : **unitaires**, **intégration**, **cas limites**,
> **régression**.

## Pré-requis

Les tests s'exécutent dans l'environnement backend complet (cv2, numpy,
ultralytics installés). Depuis `Backend/` :

```bash
source venv/bin/activate            # ou venv\Scripts\activate sous Windows
pip install -r requirements-dev.txt
pytest
```

> Lancer **depuis `Backend/`** (le `conftest.py` ajoute ce dossier au `sys.path`).
> Aucun accès réseau, DB, broker MQTT ou caméra réel n'est requis.

## Contenu

| Fichier | Cible | Type |
|---|---|---|
| `test_cadence_helpers.py` | `compute_avg_delta_seconds`, `compute_anomaly`, `compute_cadence_status`, `make_detector` | unitaire + cas limites + **régression** (OPM/statut NULL) |
| `test_stream_source.py` | `_is_file_source` | unitaire + cas limites (webcam, RTSP, Windows, file://) |
| `test_mqtt_topics.py` | `_sanitize_topic_segment`, `build_cadence_topic`, `build_config_topic` | unitaire (pas de réseau) |
| `test_tracking.py` | `LineCrosser` | unitaire + **régression** (anti-double-comptage) |
| `test_centroid_tracker.py` | `_CentroidTracker` | unitaire + cas limites (oubli, continuité) |
| `test_detection_opencv.py` | `OpenCvDetector` (pipeline MOG2/contours) | **intégration** (images synthétiques) |
| `test_api.py` | endpoints FastAPI | **intégration** (TestClient, auth 401, override + DB factice) |

## Couverture par catégorie demandée

- **Unitaires** : helpers cadence, builders MQTT, source stream, LineCrosser, centroid tracker.
- **Intégration** : pipeline OpenCV bout-à-bout sur images, endpoints API via TestClient.
- **Cas limites** : 0/1 timestamp, baseline vide/nulle, bornes inclusives, blob sous `min_area`, disparition prolongée, segments topic vides/wildcards.
- **Régression** : OPM NULL si < 2 objets ; `cadence_status` NULL sans plage de référence ; un objet compté une seule fois par itération.

## Sélection

```bash
pytest tests/test_tracking.py            # un fichier
pytest -k "anomaly or topic"             # par mot-clé
pytest -v                                # détaillé
```
