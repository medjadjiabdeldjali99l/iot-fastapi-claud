"""Publisher MQTT vers Odoo — envoi des moyennes de cadence par intervalle.

Aligné sur la convention de l'équipe IoT Miniros (cf. dossier
`Connection to odoo/`) :

* Broker : Mosquitto sur Raspberry Pi (par défaut 192.168.137.68:1883),
  LAN interne, sans TLS, sans authentification.
* Topic pattern : ``Miniros/{factory}/{line}/{machine}/{subtopic}``.
  Le sous-topic propre à cette source est ``cadence/iteration``.
* Bibliothèque : paho-mqtt, comme l'utilitaire ``broker_client()`` du
  collègue (voir ``Connection to odoo/Module odoo/iot_nodes/utils.py``).

Mécanique :

1. Au démarrage de l'app (lifespan FastAPI), on initialise un client paho
   persistant en arrière-plan (``loop_start()`` lance un thread de réseau
   non bloquant). Pas d'erreur si le broker est injoignable au boot — le
   client se reconnectera tout seul.
2. À chaque fin de fenêtre de mesure, le ``SessionRunner`` appelle
   :func:`publish_cadence_iteration` qui ne fait que mettre le message
   dans la queue de sortie du client (``client.publish()`` est non
   bloquant ; le retour ``MQTTMessageInfo`` est rangé sans attente).
3. Au shutdown, on arrête proprement le loop et on déconnecte.

Tout est best-effort : si le broker n'est pas joignable, ou si le module
``paho.mqtt`` n'est pas installé, l'envoi est silencieusement skippé
(warning loggé) — la mesure de cadence reste intacte côté DB.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.config import settings

logger = logging.getLogger(__name__)

# Le client paho est volontairement importé en lazy pour que le module
# reste importable même si paho n'est pas installé (env minimal de tests).
try:
    import paho.mqtt.client as mqtt
    _PAHO_AVAILABLE = True
except ImportError:  # pragma: no cover
    mqtt = None  # type: ignore[assignment]
    _PAHO_AVAILABLE = False


_client: "mqtt.Client | None" = None
_client_lock = threading.Lock()


def _build_client() -> "mqtt.Client | None":
    """Crée et configure un client paho prêt à se connecter. Renvoie None si
    paho n'est pas dispo ou si le module est désactivé via settings."""
    if not _PAHO_AVAILABLE or mqtt is None:
        logger.warning(
            "paho-mqtt n'est pas installé — publication MQTT désactivée"
        )
        return None
    if not settings.MQTT_ENABLED:
        logger.info("MQTT_ENABLED=false — publication MQTT désactivée")
        return None

    client = mqtt.Client(
        client_id=settings.MQTT_CLIENT_ID,
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        protocol=mqtt.MQTTv311,
        clean_session=True,
    )
    # Reconnexion automatique : delai exponentiel entre 1 s et 120 s
    # (mêmes valeurs que l'utilitaire broker_client du collègue).
    client.reconnect_delay_set(min_delay=1, max_delay=120)

    if settings.MQTT_USERNAME:
        client.username_pw_set(
            settings.MQTT_USERNAME,
            settings.MQTT_PASSWORD or None,
        )

    def on_connect(_client, _userdata, _flags, reason_code, _properties=None):
        rc = getattr(reason_code, "value", reason_code)
        if rc == 0:
            logger.info(
                "MQTT connecté à %s:%s en tant que %s",
                settings.MQTT_BROKER_HOST,
                settings.MQTT_BROKER_PORT,
                settings.MQTT_CLIENT_ID,
            )
        else:
            logger.warning("MQTT connexion refusée — reason_code=%s", rc)

    def on_disconnect(_client, _userdata, _flags=None, reason_code=None, _props=None):
        logger.warning("MQTT déconnecté — reason_code=%s", reason_code)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    return client


def init_mqtt_client() -> None:
    """Initialise le client global et lance la boucle réseau.

    Appelée au startup FastAPI (``lifespan``). Idempotente.
    Une erreur de connexion initiale n'est pas fatale : paho reconnectera
    en arrière-plan dès que le broker sera joignable."""
    global _client
    with _client_lock:
        if _client is not None:
            return
        client = _build_client()
        if client is None:
            return
        try:
            client.connect_async(
                settings.MQTT_BROKER_HOST,
                settings.MQTT_BROKER_PORT,
                settings.MQTT_KEEPALIVE,
            )
            client.loop_start()
            _client = client
            logger.info(
                "MQTT publisher démarré (broker=%s:%s prefix=%s)",
                settings.MQTT_BROKER_HOST,
                settings.MQTT_BROKER_PORT,
                settings.MQTT_TOPIC_PREFIX,
            )
        except Exception as exc:
            logger.warning("MQTT init échoué (%s) — publication désactivée", exc)
            _client = None


def shutdown_mqtt_client() -> None:
    """Arrête proprement le client. Appelée au shutdown FastAPI."""
    global _client
    with _client_lock:
        if _client is None:
            return
        try:
            _client.loop_stop()
            _client.disconnect()
        except Exception as exc:
            logger.warning("MQTT shutdown a levé : %s", exc)
        finally:
            _client = None
            logger.info("MQTT publisher arrêté")


def _sanitize_topic_segment(value: str | None, fallback: str) -> str:
    """Topic Mosquitto : pas de '/' ni de '+' ni de '#' dans un segment.
    On remplace par '_' et on tombe sur ``fallback`` si vide."""
    if not value:
        return fallback
    cleaned = (
        value.strip().replace("/", "_").replace("+", "_").replace("#", "_")
    )
    return cleaned or fallback


def _build_topic(
    factory: str | None,
    line: str | None,
    machine: str | None,
    subtopic: str,
) -> str:
    """Construit ``{prefix}/{factory}/{line}/{machine}/{subtopic}``.

    ``factory`` / ``line`` / ``machine`` peuvent être None, auquel cas on
    tombe sur les fallbacks de settings (``MQTT_FACTORY_FALLBACK`` /
    ``MQTT_LINE_FALLBACK``) ou ``"unknown"`` pour la machine."""
    f = _sanitize_topic_segment(factory, settings.MQTT_FACTORY_FALLBACK)
    l = _sanitize_topic_segment(line, settings.MQTT_LINE_FALLBACK)
    m = _sanitize_topic_segment(machine, "unknown")
    return f"{settings.MQTT_TOPIC_PREFIX}/{f}/{l}/{m}/{subtopic}"


def build_cadence_topic(
    factory: str | None,
    line: str | None,
    machine: str | None,
) -> str:
    """Topic ``{prefix}/{factory}/{line}/{machine}/cadence/iteration``."""
    return _build_topic(factory, line, machine, "cadence/iteration")


def build_config_topic(
    factory: str | None,
    line: str | None,
    machine: str | None,
) -> str:
    """Topic ``{prefix}/{factory}/{line}/{machine}/config/saved``."""
    return _build_topic(factory, line, machine, "config/saved")


def publish_cadence_iteration(
    *,
    factory: str | None,
    line: str | None,
    machine: str | None,
    session_id: UUID,
    iteration_number: int,
    cadence_moyenne: float | None,
    temps_debut: datetime | None,
    temps_fin: datetime | None,
    object_count: int,
    avg_delta_seconds: float | None = None,
    cadence_status: str | None = None,
) -> None:
    """Publie une itération de cadence sur le broker MQTT.

    Best-effort : aucune exception n'est propagée. Le payload est en JSON,
    aligné sur la spec demandée (``cadence_moyenne`` + ``temps_debut`` +
    ``temps_fin``) avec quelques champs de contexte (session_id,
    iteration_number, object_count) utiles côté Odoo pour rapprocher
    plusieurs itérations d'un même run."""
    if _client is None:
        return  # publication désactivée (paho absent, settings off, init KO)

    topic = build_cadence_topic(factory, line, machine)
    payload: dict[str, Any] = {
        "session_id": str(session_id),
        "iteration_number": iteration_number,
        "cadence_moyenne": cadence_moyenne,
        "temps_debut": temps_debut.isoformat() if temps_debut else None,
        "temps_fin": temps_fin.isoformat() if temps_fin else None,
        "object_count": object_count,
        "avg_delta_seconds": avg_delta_seconds,
        "cadence_status": cadence_status,
    }
    body = json.dumps(payload, separators=(",", ":"))

    try:
        info = _client.publish(
            topic,
            payload=body,
            qos=settings.MQTT_QOS,
            retain=settings.MQTT_RETAIN,
        )
        # rc=0 => MQTT_ERR_SUCCESS ; sinon le message a été rejeté
        # (queue pleine, client déconnecté…). On ne bloque pas.
        rc = getattr(info, "rc", 0)
        if rc != 0:
            logger.warning(
                "MQTT publish a renvoyé rc=%s (topic=%s session=%s iter=%s)",
                rc, topic, session_id, iteration_number,
            )
        else:
            logger.info(
                "MQTT publish OK topic=%s session=%s iter=%s opm=%s",
                topic, session_id, iteration_number, cadence_moyenne,
            )
    except Exception as exc:
        # Ne JAMAIS faire échouer le runner à cause de MQTT.
        logger.warning(
            "MQTT publish a levé une exception (silencieux) : %s", exc
        )


def publish_camera_config(
    *,
    factory: str | None,
    line: str | None,
    machine: str | None,
    camera_id: UUID,
    config_id: UUID,
    trigger_line_position: float,
    yolo_confidence: float,
    yolo_model: str,
    is_active: bool,
    created_at: datetime | None = None,
) -> None:
    """Publie l'événement « configuration caméra sauvegardée » sur MQTT.

    Même mécanique best-effort que :func:`publish_cadence_iteration` :
    aucune exception n'est propagée, et si le client est absent (paho non
    installé / ``MQTT_ENABLED=false`` / init KO) l'appel est un no-op.

    Déclenché à chaque ``Save config`` côté admin (nouvelle ligne active
    dans ``camera_configs``). Permet à Odoo de connaître les paramètres de
    détection courants de la caméra (ligne de trigger, modèle, confiance)."""
    if _client is None:
        return  # publication désactivée (paho absent, settings off, init KO)

    topic = build_config_topic(factory, line, machine)
    payload: dict[str, Any] = {
        "camera_id": str(camera_id),
        "config_id": str(config_id),
        "trigger_line_position": trigger_line_position,
        "yolo_confidence": yolo_confidence,
        "yolo_model": yolo_model,
        "is_active": is_active,
        "created_at": created_at.isoformat() if created_at else None,
    }
    body = json.dumps(payload, separators=(",", ":"))

    try:
        info = _client.publish(
            topic,
            payload=body,
            qos=settings.MQTT_QOS,
            retain=settings.MQTT_RETAIN,
        )
        rc = getattr(info, "rc", 0)
        if rc != 0:
            logger.warning(
                "MQTT publish config a renvoyé rc=%s (topic=%s camera=%s)",
                rc, topic, camera_id,
            )
        else:
            logger.info(
                "MQTT publish config OK topic=%s camera=%s model=%s",
                topic, camera_id, yolo_model,
            )
    except Exception as exc:
        # Ne JAMAIS faire échouer l'enregistrement de config à cause de MQTT.
        logger.warning(
            "MQTT publish config a levé une exception (silencieux) : %s", exc
        )
