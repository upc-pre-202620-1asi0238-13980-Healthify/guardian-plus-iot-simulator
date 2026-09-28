"""Publicacion de la telemetria Guardian+ hacia el broker MQTT.

Publica en un topico por canal (`<prefix>/<canal>/<deviceId>`) para que cada
Bounded Context se suscriba solo a lo suyo, y lleva la cuenta de lo que
realmente salio del cliente, no solo de lo que se intento enviar.
"""

import json
import logging
import threading
from collections import Counter, deque
from datetime import datetime, timezone
from typing import List, Optional

import paho.mqtt.client as mqtt

from model.signal import CHANNELS, Signal

logger = logging.getLogger(__name__)

RECENT_SIGNALS_SIZE = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rc_failed(reason_code) -> bool:
    """paho 2.x entrega objetos ReasonCode; las versiones viejas, ints."""
    if hasattr(reason_code, "is_failure"):
        return bool(reason_code.is_failure)
    return int(reason_code) != 0


class MqttPublisher:
    """Publica señales al broker y registra que fue enviado de verdad."""

    def __init__(self, host: str, port: int = 1883, topic_prefix: str = "guardian",
                 alerts_qos: int = 1, telemetry_qos: int = 0):
        self._host = host
        self._port = port
        self._topic_prefix = topic_prefix.rstrip("/")
        self._alerts_qos = alerts_qos
        self._telemetry_qos = telemetry_qos
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_publish = self._on_publish

        self._lock = threading.Lock()
        self._connected = False
        self._connected_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._published_count = 0
        self._failed_count = 0
        self._delivered_count = 0
        self._last_publish_at: Optional[str] = None
        self._by_channel = Counter()
        self._by_signal_type = Counter()
        self._alerts_count = 0
        self._recent: deque = deque(maxlen=RECENT_SIGNALS_SIZE)
        self._pending_mids = {}
        self._early_acks = set()

    # ---------- conexion ----------

    def connect(self):
        """Conecta en segundo plano: si el broker no esta arriba, paho reintenta
        solo y el simulador sigue en pie (el CLI muestra connected: no)."""
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.connect_async(self._host, self._port, keepalive=60)
        self._client.loop_start()
        logger.info("MQTT connecting to %s:%s in the background", self._host, self._port)

    def disconnect(self):
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        ok = not _rc_failed(reason_code)
        with self._lock:
            self._connected = ok
            if ok:
                self._connected_at = _now()
                self._last_error = None
            else:
                self._last_error = f"connect refused: {reason_code}"
        logger.info("MQTT connect to %s:%s -> %s", self._host, self._port, reason_code)

    def _on_disconnect(self, client, userdata, *args):
        reason_code = args[1] if len(args) > 1 else (args[0] if args else "unknown")
        with self._lock:
            self._connected = False
            self._last_error = f"disconnected: {reason_code}"
        logger.warning("MQTT disconnected from %s:%s (%s)", self._host, self._port, reason_code)

    def _on_publish(self, client, userdata, mid, reason_code=None, properties=None):
        """El broker/red confirmo que el mensaje salio del cliente."""
        with self._lock:
            self._delivered_count += 1
            record = self._pending_mids.pop(mid, None)
            if record is not None:
                record["delivered"] = True
                record["deliveredAt"] = _now()
            else:
                # el ack llego antes de que publish_signal registrara el mid
                self._early_acks.add(mid)

    # ---------- publicacion ----------

    def topic_for(self, signal: Signal) -> str:
        return f"{self._topic_prefix}/{signal.channel}/{signal.device_id}"

    def publish_signal(self, signal: Signal) -> dict:
        topic = self.topic_for(signal)
        # las alertas van con QoS 1: perder una caida o un SOS no es opcion
        qos = self._alerts_qos if signal.severity == "CRITICAL" else self._telemetry_qos
        payload = json.dumps(signal.to_dict())
        info = self._client.publish(topic, payload, qos=qos)
        sent = info.rc == mqtt.MQTT_ERR_SUCCESS

        record = {
            "topic": topic,
            "deviceId": signal.device_id,
            "careRecipientProfileId": signal.care_recipient_profile_id,
            "channel": signal.channel,
            "signalType": signal.signal_type,
            "severity": signal.severity,
            "summary": signal.summary(),
            "payload": signal.to_dict(),
            "qos": qos,
            "timestamp": signal.timestamp,
            "mid": info.mid,
            "accepted": sent,
            "delivered": False,
            "deliveredAt": None,
            "error": None if sent else mqtt.error_string(info.rc),
        }

        with self._lock:
            if sent:
                self._published_count += 1
                self._last_publish_at = record["timestamp"]
                self._by_channel[signal.channel] += 1
                self._by_signal_type[signal.signal_type] += 1
                if signal.severity == "CRITICAL":
                    self._alerts_count += 1
                if info.mid in self._early_acks:
                    self._early_acks.discard(info.mid)
                    record["delivered"] = True
                    record["deliveredAt"] = _now()
                else:
                    self._pending_mids[info.mid] = record
            else:
                self._failed_count += 1
                self._last_error = f"publish failed: {record['error']}"
            self._recent.appendleft(record)

        if sent:
            log = logger.warning if signal.severity == "CRITICAL" else logger.info
            log("Published %s %s (topic %s, qos %s, mid %s)",
                signal.signal_type, signal.summary(), topic, qos, info.mid)
        else:
            logger.error("NOT published: %s for %s (%s)",
                         signal.signal_type, signal.device_id, record["error"])

        return record

    def publish_all(self, signals: List[Signal]) -> List[dict]:
        return [self.publish_signal(s) for s in signals]

    # ---------- introspeccion ----------

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def topic_prefix(self) -> str:
        return self._topic_prefix

    def stats(self) -> dict:
        with self._lock:
            return {
                "host": self._host,
                "port": self._port,
                "topicPrefix": self._topic_prefix,
                "connected": self._connected,
                "connectedAt": self._connected_at,
                "published": self._published_count,
                "delivered": self._delivered_count,
                "failed": self._failed_count,
                "pending": len(self._pending_mids),
                "criticalAlerts": self._alerts_count,
                "byChannel": {c: self._by_channel.get(c, 0) for c in CHANNELS},
                "bySignalType": dict(self._by_signal_type.most_common()),
                "lastPublishAt": self._last_publish_at,
                "lastError": self._last_error,
            }

    def recent_signals(self, limit: int = 10, channel: Optional[str] = None,
                       device_id: Optional[str] = None,
                       severity: Optional[str] = None) -> List[dict]:
        with self._lock:
            records = list(self._recent)
        if channel:
            records = [r for r in records if r["channel"] == channel]
        if device_id:
            records = [r for r in records if r["deviceId"] == device_id]
        if severity:
            records = [r for r in records if r["severity"] == severity.upper()]
        return [dict(r) for r in records[:limit]]
