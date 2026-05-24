from enum import Enum

from pydantic import BaseModel, ConfigDict


class CameraStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class YoloModel(str, Enum):
    V8N = "yolov8n"
    V8L = "yolov8l"
    V11N = "yolov11n"
    V11L = "yolov11l"


class SessionMode(str, Enum):
    SINGLE = "single"
    INTERVAL = "interval"


class SessionStatus(str, Enum):
    PENDING = "pending"
    WAITING_FIRST = "waiting_first"
    WAITING_SECOND = "waiting_second"
    PAUSED = "paused"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"


class UserRole(str, Enum):
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class CrossingType(str, Enum):
    T0 = "T0"
    T1 = "T1"


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
