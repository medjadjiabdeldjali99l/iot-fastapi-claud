from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    APP_NAME: str = "IoT Cadence Backend"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = ""
    SUPABASE_SERVICE_KEY: str = ""
    SUPABASE_JWT_SECRET: str = ""

    CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
    ]

    YOLO_DEFAULT_MODEL: str = "yolov8n"
    YOLO_DEFAULT_CONFIDENCE: float = 0.5

    # Moteur de détection utilisé pour le comptage / la cadence :
    #   "yolo"   → réseau Ultralytics + ByteTrack (précis, lourd sur CPU/Pi)
    #   "opencv" → vision classique MOG2 + contours + centroid tracker (léger,
    #              temps réel sur Pi). Voir services/detection_opencv.py.
    # Bascule sans toucher au code, juste via .env. Interface identique des
    # deux côtés, donc le reste du runner ne change pas.
    DETECTOR_BACKEND: str = "yolo"

    # --- Réglages du détecteur OpenCV (ignorés si DETECTOR_BACKEND=yolo) ---
    # Tunables sans recompiler — voir services/detection_opencv.py.
    # Filtrage des contours (en px²) :
    OPENCV_MIN_AREA: int = 500          # aire mini d'un blob pour le garder
    OPENCV_MAX_AREA: int = 0            # aire maxi (0 = pas de plafond)
    # Soustraction de fond MOG2 :
    OPENCV_MOG2_HISTORY: int = 500      # nb de frames pour modéliser le fond
    OPENCV_MOG2_VAR_THRESHOLD: float = 16.0  # sensibilité (bas = + sensible)
    OPENCV_MOG2_DETECT_SHADOWS: bool = True  # marquer/retirer les ombres
    # Centroid tracker :
    OPENCV_TRACK_MAX_DISAPPEARED: int = 30   # frames d'absence avant oubli d'un id
    OPENCV_TRACK_MAX_DISTANCE: float = 80.0  # distance max (px) pour réassocier un id

    ANOMALY_THRESHOLD_PCT: float = 15.0
    CAMERA_PING_TIMEOUT_SECONDS: float = 3.0

    # --- MQTT (envoi des moyennes de cadence vers Odoo) ---
    # Convention de l'équipe IoT Miniros : broker Mosquitto sur le Raspberry Pi
    # (192.168.137.68:1883 en LAN, sans TLS), topic pattern
    # `Miniros/{factory}/{line}/{machine}/{subtopic}`. Le sous-topic utilisé
    # par ce backend est `cadence/iteration` (payload JSON, voir
    # services/mqtt_publisher.py).
    MQTT_ENABLED: bool = False
    MQTT_BROKER_HOST: str = "192.168.137.68"
    MQTT_BROKER_PORT: int = 1883
    MQTT_USERNAME: str = ""
    MQTT_PASSWORD: str = ""
    MQTT_CLIENT_ID: str = "miniros-camera-backend"
    MQTT_KEEPALIVE: int = 60
    MQTT_TOPIC_PREFIX: str = "Miniros"
    # Valeurs par défaut si la caméra n'a pas de location renseignée (line)
    # ou si l'organisation n'a pas de slug.
    MQTT_FACTORY_FALLBACK: str = "default"
    MQTT_LINE_FALLBACK: str = "lineA"
    MQTT_QOS: int = 0
    MQTT_RETAIN: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
