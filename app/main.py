import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.routers import cadence, cameras, health, streams
from app.services.mqtt_publisher import init_mqtt_client, shutdown_mqtt_client
from app.websockets import stream_ws

# Root à WARNING — silence httpx / supabase / ultralytics qui sont très verbeux
# en INFO. Les traces de cadence (t0, t1, … et le résumé) passent par print()
# donc s'affichent quoi qu'il arrive.
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
# Silence aussi les loggers qui n'héritent pas du root :
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)  # polls du frontend
for noisy in ("httpx", "httpcore", "hpack", "h2"):              # appels REST Supabase
    logging.getLogger(noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Démarre le publisher MQTT vers Odoo (si MQTT_ENABLED=true). Best-effort :
    # si le broker n'est pas joignable, l'app démarre quand même et paho
    # reconnectera plus tard. Voir services/mqtt_publisher.py.
    init_mqtt_client()
    try:
        yield
    finally:
        shutdown_mqtt_client()


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    debug=settings.DEBUG,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(cameras.router)
app.include_router(cadence.router)
app.include_router(streams.router)
app.include_router(stream_ws.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
    )
