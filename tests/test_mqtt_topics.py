"""Tests unitaires de la construction des topics MQTT et de l'assainissement
des segments. N'ouvre aucune connexion réseau (fonctions pures)."""
from app.core.config import settings
from app.services.mqtt_publisher import (
    _sanitize_topic_segment,
    build_cadence_topic,
    build_config_topic,
)


# --- _sanitize_topic_segment ---------------------------------------------

def test_sanitize_keeps_clean_value():
    assert _sanitize_topic_segment("usineA", "fb") == "usineA"


def test_sanitize_replaces_mqtt_wildcards_and_slash():
    # '/', '+', '#' interdits dans un segment -> remplacés par '_'
    assert _sanitize_topic_segment("a/b+c#d", "fb") == "a_b_c_d"


def test_sanitize_none_uses_fallback():
    assert _sanitize_topic_segment(None, "fb") == "fb"


def test_sanitize_blank_uses_fallback():
    assert _sanitize_topic_segment("   ", "fb") == "fb"


# --- build_cadence_topic / build_config_topic ----------------------------

def test_cadence_topic_structure():
    topic = build_cadence_topic("fac", "lin", "mac")
    assert topic == f"{settings.MQTT_TOPIC_PREFIX}/fac/lin/mac/cadence/iteration"


def test_config_topic_structure():
    topic = build_config_topic("fac", "lin", "mac")
    assert topic == f"{settings.MQTT_TOPIC_PREFIX}/fac/lin/mac/config/saved"


def test_topic_uses_fallbacks_when_segments_none():
    topic = build_cadence_topic(None, None, None)
    expected = (
        f"{settings.MQTT_TOPIC_PREFIX}/{settings.MQTT_FACTORY_FALLBACK}/"
        f"{settings.MQTT_LINE_FALLBACK}/unknown/cadence/iteration"
    )
    assert topic == expected


def test_topic_segments_are_sanitized():
    topic = build_config_topic("a/b", "c+d", "e#f")
    assert topic == f"{settings.MQTT_TOPIC_PREFIX}/a_b/c_d/e_f/config/saved"
