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


class SessionStatus(str, Enum):
    PENDING = "pending"
    MEASURING = "measuring"
    PAUSED = "paused"
    STOPPED = "stopped"
    FAILED = "failed"


class UserRole(str, Enum):
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class CadenceStatus(str, Enum):
    BELOW = "below"
    NORMAL = "normal"
    ABOVE = "above"


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)