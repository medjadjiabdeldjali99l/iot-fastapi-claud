# Documentation technique — IoT Cadence (Backend)

> Étape 1/3 — Documentation **technique**. Couvre l'architecture, les fonctions,
> l'installation, le schéma de données, l'API et le déploiement.
> Version : branche `opencv-no-yolo` (détecteur YOLO **ou** OpenCV commutable).

---

## 1. Architecture générale

### 1.1 Vue d'ensemble

Système de monitoring de **cadence de production** : une caméra filme la fin d'une
chaîne ; un détecteur compte les objets qui franchissent une ligne verticale ; on
calcule la cadence moyenne (objets/minute = **OPM**) par fenêtre de mesure ; les
résultats sont stockés dans Supabase et publiés sur MQTT pour Odoo.

```
                         ┌────────────────────────────────────────┐
   Caméra / vidéo  ──────►            BACKEND (FastAPI)            │
   (RTSP/USB/fichier)    │                                          │
                         │  stream → détecteur → tracking → ligne   │
                         │     → calcul cadence → DB + MQTT         │
                         └───┬───────────────┬──────────────┬──────┘
                             │ REST + WS      │ supabase-py   │ paho-mqtt
                             ▼                ▼               ▼
                   Frontend web/mobile   Supabase (PG+RLS)   Mosquitto → Odoo
```

### 1.2 Composants

| Composant | Techno | Rôle |
|---|---|---|
| **Backend** | FastAPI + Pydantic v2 + asyncio | API REST, WebSocket preview, runner de cadence in-process |
| **Détection** | OpenCV (MOG2) **ou** Ultralytics YOLO + ByteTrack | détection d'objets par frame |
| **Tracking** | centroid tracker maison (OpenCV) / ByteTrack (YOLO) | identité stable des objets entre frames |
| **DB** | Supabase (PostgreSQL + Auth + RLS + Realtime) | multi-tenant, historique, sessions, itérations |
| **MQTT** | paho-mqtt → Mosquitto | publication cadence + config vers Odoo |
| **Frontend web** | React 18 + Vite + TS | dashboard admin |
| **Frontend mobile** | Expo + React Native | app mobile |

### 1.3 Modules backend (`Backend/app/`)

```
app/
├── main.py                  # création FastAPI, logging, lifespan (MQTT), CORS, routers
├── core/config.py           # Settings (pydantic-settings, lecture .env)
├── db/
│   ├── supabase.py          # clients Supabase (anon / service_role), cache lru
│   └── deps.py              # auth JWT, dépendances FastAPI (AuthDep, AdminDB)
├── models/                  # schémas Pydantic (I/O API)
│   ├── common.py            # enums + ORMModel
│   ├── camera.py, camera_config.py, session.py, iteration.py
├── routers/
│   ├── health.py            # GET /health
│   ├── cameras.py           # /cameras (status, configs) + MQTT config/saved
│   ├── cadence.py           # /sessions (CRUD + lancement runner)
│   └── streams.py           # WS /ws/sessions/{id} (stub)
├── websockets/
│   └── stream_ws.py         # WS /ws/cameras/{id}/preview (JPEG base64)
└── services/
    ├── stream.py            # RTSPStream — capture OpenCV (RTSP/USB/fichier)
    ├── detection.py         # YoloDetector (Ultralytics + ByteTrack)
    ├── detection_opencv.py  # OpenCvDetector (MOG2 + contours + centroid tracker)
    ├── tracking.py          # LineCrosser — franchissement de ligne
    ├── cadence.py           # SessionRunner + registre + fabrique de détecteur
    ├── camera_ping.py       # ping caméra (online/offline)
    └── mqtt_publisher.py    # client paho + publish cadence/config
```

### 1.4 Flux d'une session de cadence

1. `POST /sessions` → crée la ligne `sessions`, instancie un `SessionRunner`, l'enregistre dans le registre in-process, lance la tâche asyncio.
2. Le runner boucle : pour chaque itération
   - ouvre le flux (`RTSPStream`),
   - pendant `measurement_window_seconds`, lit les frames → détecteur → `LineCrosser` → collecte les timestamps de franchissement,
   - calcule `avg_delta_seconds` puis `opm = 60/avg_delta`,
   - compare à la plage de référence → `cadence_status`,
   - écrit l'itération en DB (trigger SQL recalcule `opm`),
   - publie sur MQTT (`cadence/iteration`),
   - détecte une anomalie vs moyenne du run,
   - pause `interval_minutes`, recommence.
3. `POST /sessions/{id}/stop` → annule la tâche ; le runner marque la session `stopped`.

### 1.5 Limitations d'architecture connues

- **Runner in-process, single-worker** : les runners vivent dans la mémoire du
  process. Un redémarrage backend perd les sessions actives (elles restent
  `measuring`/`paused` en DB jusqu'à nettoyage externe). Ne pas lancer uvicorn
  avec `--workers > 1`.
- **Horodatage des franchissements = horloge réelle** (`datetime.now`). Sur un
  **fichier vidéo** traité plus lentement que le temps réel, la cadence est
  sous-estimée. Sans effet sur une **caméra temps réel**.

---

## 2. Description des fonctions / méthodes

### 2.1 `core/config.py`

**`Settings`** (pydantic-settings, lit `.env`). Champs clés :

| Champ | Type | Défaut | Rôle |
|---|---|---|---|
| `HOST` / `PORT` | str / int | `0.0.0.0` / `8000` | bind serveur |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` / `SUPABASE_SERVICE_KEY` / `SUPABASE_JWT_SECRET` | str | — | accès Supabase |
| `CORS_ORIGINS` | list[str] | localhost:3000/5173 | origines autorisées |
| `YOLO_DEFAULT_MODEL` / `YOLO_DEFAULT_CONFIDENCE` | str / float | yolov8n / 0.5 | défauts YOLO |
| `DETECTOR_BACKEND` | str | `yolo` | moteur : `yolo` \| `opencv` |
| `OPENCV_MIN_AREA` / `OPENCV_MAX_AREA` | int | 500 / 0 | filtre aire contour (px²) ; 0 = pas de plafond |
| `OPENCV_MOG2_HISTORY` / `OPENCV_MOG2_VAR_THRESHOLD` / `OPENCV_MOG2_DETECT_SHADOWS` | int / float / bool | 500 / 16.0 / true | réglages MOG2 |
| `OPENCV_TRACK_MAX_DISAPPEARED` / `OPENCV_TRACK_MAX_DISTANCE` | int / float | 30 / 80.0 | centroid tracker |
| `ANOMALY_THRESHOLD_PCT` | float | 15.0 | seuil anomalie |
| `CAMERA_PING_TIMEOUT_SECONDS` | float | 3.0 | timeout ping |
| `MQTT_ENABLED` | bool | false | active la publication |
| `MQTT_BROKER_HOST` / `MQTT_BROKER_PORT` | str / int | 192.168.137.68 / 1883 | broker |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | str | "" | auth (vide en LAN) |
| `MQTT_CLIENT_ID` / `MQTT_KEEPALIVE` | str / int | miniros-camera-backend / 60 | client |
| `MQTT_TOPIC_PREFIX` | str | Miniros | préfixe topic |
| `MQTT_FACTORY_FALLBACK` / `MQTT_LINE_FALLBACK` | str | default / lineA | fallback segments topic |
| `MQTT_QOS` / `MQTT_RETAIN` | int / bool | 0 / false | options publish |

- **`get_settings() -> Settings`** : instance unique mise en cache (`@lru_cache`). Exposée par `settings`.

### 2.2 `db/supabase.py`

- **`get_supabase_client() -> Client`** : client avec clé **anon** (cache lru).
- **`get_supabase_admin() -> Client`** : client avec clé **service_role** (bypass RLS).

### 2.3 `db/deps.py` — Authentification

- **`decode_supabase_jwt(token: str) -> dict`** : vérifie un JWT Supabase. Supporte **HS256** (via `SUPABASE_JWT_SECRET`) et **ES256/RS256** (via JWKS `<URL>/auth/v1/.well-known/jwks.json`, cache 1 h). **Lève** `JWTError` si invalide/alg non supporté.
- **`supabase_client_for_token(token) -> Client`** : client Supabase par requête portant le JWT (RLS évaluée sous l'identité réelle). **Lève** `RuntimeError` si credentials absents.
- **`get_auth(authorization: Header) -> Auth`** : dépendance FastAPI. Extrait le Bearer, décode le JWT, renvoie `Auth(user_id, db)`. **Lève** `HTTPException 401` si header manquant, token invalide, ou claim `sub` absent.
- **`get_admin_db() -> Client`** : renvoie le client service_role.
- Types exportés : **`AuthDep`** = `Annotated[Auth, Depends(get_auth)]`, **`AdminDB`**.
- **`Auth`** (dataclass) : `user_id: UUID`, `db: Client`.

### 2.4 `services/stream.py` — Capture vidéo

- **`_is_file_source(url) -> bool`** : `True` si l'URL est un chemin de fichier (pas un chiffre webcam, pas http/https/rtsp/rtmp). Gère `file://` et le cas Windows `C:\...`.
- **`RTSPStream(url, target_fps=10, jpeg_quality=70)`** : wrapper async autour de `cv2.VideoCapture`.
  - **`_open_capture() -> VideoCapture`** : ouvre webcam (`url` chiffre → index), fichier (backend FFmpeg), ou flux réseau (timeouts FFmpeg 5 s). Privé.
  - **`raw_frames() -> AsyncIterator[np.ndarray]`** : yield des frames BGR. Cadence : **fps natif** pour un fichier, sinon `target_fps`. Un fichier **rejoue en boucle** (seek 0 à l'EOF). Libère la capture en `finally`. **Lève** `StreamUnavailable` si ouverture impossible, signal perdu (flux réseau), ou fichier illisible après seek.
  - **`frames() -> AsyncIterator[bytes]`** : vue JPEG de `raw_frames()` (pour le WS preview).
  - **`close()`** : libération idempotente de la capture.
- **Exceptions** : `StreamError` (base), `StreamUnavailable`.

### 2.5 `services/detection.py` — Détecteur YOLO

- **`Detection`** (dataclass) — **type de sortie commun aux deux détecteurs** : `tracker_id: int|None`, `bbox: (x1,y1,x2,y2)` px, `confidence: float`, `class_id: int`, `class_name: str`.
- **`YoloDetector(model_name="yolov8n")`** :
  - **`detect_sync(frame, confidence=0.5) -> list[Detection]`** : inférence sans tracking.
  - **`track_sync(frame, confidence=0.5) -> list[Detection]`** : tracking ByteTrack (`tracker_id` peuplé).
  - **`detect(...)` / `track(...)`** : versions async (exécution en thread via `asyncio.to_thread`).
  - Modèle chargé en lazy (`.pt`), protégé par un `Lock`.

### 2.6 `services/detection_opencv.py` — Détecteur OpenCV

- **`_CentroidTracker(max_disappeared=30, max_distance=80.0)`** : suivi par plus-proche-centroïde.
  - **`update(boxes) -> list[int]`** : renvoie les `tracker_id` parallèles à `boxes`. Apparie par distance euclidienne (glouton, seuil `max_distance`) ; enregistre les nouveaux ; oublie un objet absent > `max_disappeared` frames.
- **`OpenCvDetector(min_area=500, max_area=None, history=500, var_threshold=16.0, detect_shadows=True, max_disappeared=30, max_distance=80.0, class_name="object")`** :
  - **`_foreground_boxes(frame) -> list[bbox]`** : MOG2 → seuillage anti-ombres → morphologie open/close → `findContours` → filtre par aire.
  - **`detect_sync(frame, confidence=0.5) -> list[Detection]`** : détection sans `tracker_id`. `confidence` **ignoré**.
  - **`track_sync(frame, confidence=0.5) -> list[Detection]`** : détection + centroid tracking (`tracker_id` peuplé). `confidence` ignoré ; `confidence=1.0` et `class_name="object"` fixes.
  - **`detect(...)` / `track(...)`** : versions async. `Lock` (MOG2/tracker stateful).
  - **Interface strictement identique à `YoloDetector`** → interchangeable dans le runner.

### 2.7 `services/tracking.py` — Franchissement de ligne

- **`CrossingEvent`** (dataclass) : `tracker_id`, `detection`, `timestamp: datetime`, `direction`, `line_x_px`.
- **`LineCrosser(line_position=0.80, direction="any")`** : détecte le passage du **centre** d'un objet au-delà d'une ligne verticale (ratio de largeur ∈ [0,1]). **Lève** `ValueError` si `line_position` hors [0,1].
  - **`update(detections, frame_width) -> list[CrossingEvent]`** : émet **au plus un** event par `tracker_id` (re-franchissements supprimés). Horodatage = `datetime.now(utc)`. Oublie les ids sortis du cadre.
  - **`reset()`** : vide l'état.
  - **`crossed_ids` (property)** : ids déjà comptés (pour griser les boîtes dans le preview).

### 2.8 `services/cadence.py` — Runner

Helpers purs :
- **`compute_anomaly(current_opm, baseline_opms, threshold_pct) -> (bool, float) | None`** : `None` si pas de baseline ; sinon `(is_anomaly, deviation_pct)`.
- **`compute_avg_delta_seconds(timestamps) -> float | None`** : moyenne des écarts consécutifs ; `None` si < 2 timestamps.
- **`compute_cadence_status(opm, ref_min, ref_max) -> CadenceStatus | None`** : `BELOW`/`NORMAL`/`ABOVE`, ou `None` si opm/plage manquants.
- **`make_detector(yolo_model) -> YoloDetector | OpenCvDetector`** : **fabrique** selon `settings.DETECTOR_BACKEND` (`opencv` → `OpenCvDetector` avec les réglages `OPENCV_*` ; sinon YOLO). Défaut = YOLO.

**`SessionRunner(...)`** — paramètres : `session_id`, `camera_id`, `stream_url`, `trigger_line_position`, `yolo_confidence`, `yolo_model`, `db`, `interval_minutes`, `measurement_window_seconds=120`, `anomaly_threshold_pct=15.0`, `reference_cadence_min/max=None`, `target_fps=10`, `preview_jpeg_quality=70`, `mqtt_factory/line/machine=None`. **Lève** `ValueError` si `interval_minutes<=0`, `measurement_window_seconds<=0`, ou plage de référence incohérente.

Méthodes principales :
- **`start() -> asyncio.Task`** : lance la boucle (idempotent).
- **`request_stop()`** : demande l'arrêt (annule la tâche).
- **`latest_frame` (property)** : dernier JPEG annoté (consommé par le WS preview).
- **`_run()`** : enveloppe ; gère `CancelledError` (→ `stopped`), `StreamUnavailable`/`Exception` (→ `failed`), retire du registre en `finally`.
- **`_run_interval()`** : boucle d'itérations (mesure → calcul → DB → MQTT → anomalie → pause).
- **`_collect_window(...) -> list[datetime]`** : collecte les franchissements sur la fenêtre.
- **`_compute_anomaly_for(iteration_id)`**, **`_encode_annotated(...)`**, helpers DB (`_create_iteration`, `_update_iteration`, `_update_session`, `_mark_stopped`, `_fail`).

**`RunnerRegistry`** (instance `registry()`) : map in-process `session_id → runner`, protégée par `asyncio.Lock`.
- **`register / get / remove / find_by_camera`** : `find_by_camera` sert au WS preview pour partager les frames d'un runner actif.

### 2.9 `services/camera_ping.py`

- **`ping_stream_url(url, timeout=None) -> bool`** : webcam (chiffre) → `True` ; http/https → HEAD puis GET ; rtsp → TCP connect (port 554 défaut) ; `file://`/chemin → existence fichier.

### 2.10 `services/mqtt_publisher.py`

- **`init_mqtt_client()`** : crée le client paho (API v2, MQTTv311, reconnexion 1→120 s), `connect_async` + `loop_start`. Idempotent. No-op si paho absent ou `MQTT_ENABLED=false`. Erreur de connexion **non fatale**.
- **`shutdown_mqtt_client()`** : `loop_stop` + `disconnect`.
- **`build_cadence_topic(...)` / `build_config_topic(...)`** : `Miniros/{factory}/{line}/{machine}/{cadence/iteration | config/saved}`. Segments assainis (`/ + #` → `_`), fallback si vide.
- **`publish_cadence_iteration(*, factory, line, machine, session_id, iteration_number, cadence_moyenne, temps_debut, temps_fin, object_count, avg_delta_seconds=None, cadence_status=None)`** : publie le payload JSON cadence. **Best-effort : n'élève jamais d'exception** (no-op si client absent).
- **`publish_camera_config(*, factory, line, machine, camera_id, config_id, trigger_line_position, yolo_confidence, yolo_model, is_active, created_at=None)`** : publie le payload config. Idem best-effort.

### 2.11 `main.py`

- Logging : root `WARNING` ; `uvicorn.access`/`httpx`/… silencés ; `app.services.mqtt_publisher` à `INFO`.
- **`lifespan`** : `init_mqtt_client()` au startup, `shutdown_mqtt_client()` au shutdown.
- CORS via `settings.CORS_ORIGINS`. Routers : health, cameras, cadence, streams, stream_ws.

---

## 3. Installation & configuration de l'environnement

### 3.1 Prérequis

- Python **3.10+**
- (Optionnel) Mosquitto si publication MQTT
- (YOLO) poids `.pt` téléchargés au 1ᵉʳ usage par Ultralytics
- Projet **Supabase** avec le schéma appliqué (cf. §4)

### 3.2 Backend

```bash
cd Backend
python3 -m venv venv
source venv/bin/activate        # Windows : venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env            # puis renseigner (cf. §3.4)
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Dépendances clés (`requirements.txt`) : fastapi, uvicorn[standard], pydantic(-settings), supabase, httpx, python-jose[cryptography], python-multipart, **opencv-python-headless**, numpy, **ultralytics**, lap, **paho-mqtt**.

### 3.3 Base de données

Exécuter `supabase_schema.sql` dans l'éditeur SQL Supabase (idempotent). Créer un user via `scripts/create_user.py`.

### 3.4 Variables `.env` (backend)

```ini
HOST=0.0.0.0
PORT=8000
SUPABASE_URL=https://<projet>.supabase.co
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_KEY=...
SUPABASE_JWT_SECRET=...
CORS_ORIGINS=["http://<ip>:5173","http://localhost:5173"]

DETECTOR_BACKEND=yolo            # ou opencv
OPENCV_MIN_AREA=500
OPENCV_MAX_AREA=0
OPENCV_MOG2_HISTORY=500
OPENCV_MOG2_VAR_THRESHOLD=16.0
OPENCV_MOG2_DETECT_SHADOWS=true
OPENCV_TRACK_MAX_DISAPPEARED=30
OPENCV_TRACK_MAX_DISTANCE=80.0

MQTT_ENABLED=false               # true pour publier
MQTT_BROKER_HOST=localhost
MQTT_BROKER_PORT=1883
```

### 3.5 Frontend web

```bash
cd frontend-web
npm install
cp .env.example .env             # VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY / VITE_API_URL
npm run dev -- --host 0.0.0.0    # http://<ip>:5173
```

---

## 4. Schéma de base de données / modèles

### 4.1 Enums

`camera_status` (online/offline/unknown) · `yolo_model` (yolov8n/8l/11n/11l) · `session_status` (pending/measuring/paused/stopped/failed) · `user_role` (admin/operator/viewer) · `cadence_status` (below/normal/above).

### 4.2 Tables

| Table | Colonnes principales | Notes |
|---|---|---|
| **organizations** | id, name, **slug** (unique) | multi-tenant ; `slug` = `factory` MQTT |
| **profiles** | id (→ auth.users), organization_id, full_name, role | provisionné par trigger `handle_new_user` |
| **cameras** | id, organization_id, name, **location**, **stream_url**, status, last_seen_at | unique(org, name) ; `location`=`line`, `name`=`machine` MQTT |
| **camera_configs** | id, camera_id, trigger_line_position (0–1), yolo_confidence (0–1), yolo_model, **is_active**, created_by | versionné ; **1 seule active/caméra** (index partiel) |
| **sessions** | id, camera_id, config_id, organization_id, interval_minutes, measurement_window_seconds, anomaly_threshold_pct, reference_cadence_min/max, status, started_at/ended_at, started_by, notes | run récurrent ; contrainte plage réf (les deux null, ou min≤max) |
| **session_iterations** | id, session_id, iteration_number, measurement_started_at/ended_at, object_count, avg_delta_seconds, **opm**, is_anomaly, anomaly_deviation, cadence_status, completed_at | unique(session, iteration_number) ; `opm` calculé par trigger |

### 4.3 Vue

- **`session_summaries`** : par session — `iteration_count`, `avg_opm`, `min_opm`, `max_opm`, `anomaly_count`, `below_count`/`normal_count`/`above_count` (ne compte que les itérations `opm not null`).

### 4.4 Triggers / fonctions

- **`set_updated_at`** : maj `updated_at` (organizations, profiles, cameras, sessions).
- **`deactivate_other_configs`** : garantit une seule config active par caméra.
- **`compute_iteration_metrics`** : `opm = round(60/avg_delta_seconds,2)` + `completed_at` à l'insert/update d'une itération.
- **`current_user_org()` / `current_user_role()`** : helpers RLS (security definer).
- **`handle_new_user`** : crée le `profile` à la création d'un user Auth (lit `raw_user_meta_data.organization_id/full_name/role`).

### 4.5 Sécurité

- **RLS** activée sur toutes les tables métier ; lecture filtrée par `current_user_org()`, écriture réservée `admin`/`operator`.
- **GRANTs** explicites (service_role : all ; authenticated : CRUD ; anon : select) + `alter default privileges`.
- **Realtime** : `cameras`, `sessions`, `session_iterations` publiées.

### 4.6 Modèles Pydantic (I/O API)

- `ORMModel` (base, `from_attributes=True`).
- Camera : `CameraBase/Create/Update/Out`, `CameraStatusOut`.
- CameraConfig : `CameraConfigBase/Create/Out` (trigger_line_position, yolo_confidence ∈ [0,1], yolo_model).
- Session : `SessionBase` (validation plage réf), `SessionCreate` (+camera_id), `SessionOut`, `SessionSummary`.
- Iteration : `IterationOut`.

---

## 5. API Reference

**Base URL** : `http://<host>:8000`
**Auth** : header `Authorization: Bearer <supabase_jwt>` (sauf `/health`). WebSocket : `?token=<jwt>` en query (les navigateurs ne peuvent pas poser de header sur un WS).
**Erreurs** : JSON FastAPI `{"detail": "..."}`. Codes : 401 (auth), 404 (introuvable), 409 (conflit), 501 (non implémenté), 500.

### 5.1 Santé

| Méthode | Chemin | Réponse |
|---|---|---|
| GET | `/health` | `{"status":"ok","app":...,"environment":...}` |

### 5.2 Caméras (`/cameras`)

| Méthode | Chemin | Auth | Réponse / Notes |
|---|---|---|---|
| GET | `/cameras` | — | **501** (stub) |
| POST | `/cameras` | — | **501** |
| GET | `/cameras/{id}` | — | **501** |
| PATCH | `/cameras/{id}` | — | **501** |
| DELETE | `/cameras/{id}` | — | **501** |
| GET | `/cameras/{id}/status` | ✅ | `CameraStatusOut` — ping + maj `status`/`last_seen_at`. 404 si inconnue |
| POST | `/cameras/{id}/stream/open` | — | **501** |
| POST | `/cameras/{id}/stream/close` | — | **501** |
| GET | `/cameras/{id}/configs/active` | ✅ | `CameraConfigOut`. **404** si pas de config active (client → défauts 0.80/0.50/yolov8n) |
| POST | `/cameras/{id}/configs` | ✅ | `CameraConfigOut` (**201**). Crée une config active (trigger désactive l'ancienne) **+ publie MQTT `config/saved`**. 404 si caméra inconnue |

**Body `POST /cameras/{id}/configs`** :
```json
{ "trigger_line_position": 0.80, "yolo_confidence": 0.50, "yolo_model": "yolov8n" }
```

### 5.3 Sessions / Cadence (`/sessions`)

| Méthode | Chemin | Auth | Réponse / Notes |
|---|---|---|---|
| POST | `/sessions` | ✅ | `SessionOut` (**201**). Crée la session + lance le runner. 404 caméra, 409 si pas de config active |
| POST | `/sessions/{id}/stop` | ✅ | `SessionOut`. Annule le runner. 404 si session inconnue |
| GET | `/sessions/{id}` | ✅ | `SessionOut`. 404 |
| GET | `/sessions/{id}/iterations` | ✅ | `list[IterationOut]` (triées par `iteration_number`) |
| GET | `/sessions/{id}/summary` | ✅ | `SessionSummary`. 404 |
| GET | `/sessions` | ✅ | `list[SessionSummary]` (100 dernières, `started_at` desc) |

**Body `POST /sessions`** :
```json
{
  "camera_id": "<uuid>",
  "interval_minutes": 5,
  "measurement_window_seconds": 120,
  "anomaly_threshold_pct": 15.0,
  "reference_cadence_min": 20,
  "reference_cadence_max": 30,
  "notes": null
}
```

### 5.4 WebSockets

| Chemin | Query | Messages |
|---|---|---|
| `/ws/cameras/{id}/preview` | `token`, `fps` (1–30, déf. 10), `quality` (10–95, déf. 70) | `{"type":"frame","ts":...,"data":<base64 jpeg>}` / `{"type":"error","message":...}`. Si une session tourne sur la caméra → frames **annotées** du runner. Codes close : 1000/1008/1011 |
| `/ws/sessions/{id}` | — | stub (draine les messages, détecte la déconnexion) |

### 5.5 MQTT (sortant, vers Odoo)

| Topic | Déclencheur | Payload (JSON) |
|---|---|---|
| `Miniros/{factory}/{line}/{machine}/cadence/iteration` | fin de chaque fenêtre | `session_id, iteration_number, cadence_moyenne, temps_debut, temps_fin, object_count, avg_delta_seconds, cadence_status` |
| `Miniros/{factory}/{line}/{machine}/config/saved` | Save config | `camera_id, config_id, trigger_line_position, yolo_confidence, yolo_model, is_active, created_at` |

`factory`=`organizations.slug`, `line`=`cameras.location`, `machine`=`cameras.name` (fallbacks si vides). `cadence_moyenne`/`avg_delta_seconds` = `null` si < 2 objets ; `cadence_status` = `null` sans plage de référence.

---

## 6. Guide de déploiement (Raspberry Pi)

### 6.1 Réseau

- Pi et clients sur le même LAN. Récupérer l'IP : `hostname -I`.
- Ajouter l'origine du frontend (`http://<ip_pi>:5173`) dans `CORS_ORIGINS` (backend).

### 6.2 Backend

```bash
cd Backend && python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
# .env : SUPABASE_*, CORS_ORIGINS (IP Pi), DETECTOR_BACKEND, MQTT_*
uvicorn app.main:app --host 0.0.0.0 --port 8000
# vérif depuis le PC : http://<ip_pi>:8000/docs
```

### 6.3 MQTT (Mosquitto sur le Pi)

```bash
sudo apt install mosquitto mosquitto-clients
sudo systemctl enable --now mosquitto
# .env backend : MQTT_ENABLED=true, MQTT_BROKER_HOST=localhost
# écoute : mosquitto_sub -h localhost -t "Miniros/#" -v
```

### 6.4 Source caméra (`cameras.stream_url`)

- **Webcam USB** : `"0"` (index `/dev/video0`). User dans le groupe `video`.
- **Fichier vidéo** (test) : chemin absolu (rejoué en boucle).
- **RTSP/HTTP** : URL complète.

### 6.5 Frontend web

```bash
cd frontend-web && npm install
# .env : VITE_API_URL=http://<ip_pi>:8000
npm run dev -- --host 0.0.0.0
```

### 6.6 systemd (optionnel — démarrage auto backend)

Service `uvicorn app.main:app` avec `WorkingDirectory=.../Backend`, `ExecStart=.../venv/bin/uvicorn ...`, `Restart=on-failure`. **Un seul worker** (runner in-process).

### 6.7 Bascule YOLO ↔ OpenCV

`.env` → `DETECTOR_BACKEND=opencv` (ou `yolo`) → relancer uvicorn. Log de confirmation : `>>> RUNNER START ... detector=opencv`.
