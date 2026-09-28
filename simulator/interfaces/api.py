"""API HTTP de control/monitoreo del simulador y loop de emision de telemetria."""

import logging
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import requests
from flask import Flask, jsonify, request

from interfaces.mqtt_publisher import MqttPublisher
from model.signal import CHANNELS, Device
from model.signal_generator import SignalGenerator

logger = logging.getLogger(__name__)

# Lima, Peru: punto de partida de las pulseras cuando el backend no informa
# una ubicacion de referencia para la Safe Zone.
DEFAULT_HOME_LATITUDE = -12.046374
DEFAULT_HOME_LONGITUDE = -77.042793

TRIGGER_EVENTS = ("CYCLE", "VITALS", "ANOMALY", "FALL", "SOS", "LOCATION", "ACTIVITY", "SLEEP", "BATTERY")


def _first(source: dict, *keys):
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return value
    return None


def _home_coordinates(source: dict):
    latitude = _first(source, "homeLatitude", "latitude", "safeZoneLatitude")
    longitude = _first(source, "homeLongitude", "longitude", "safeZoneLongitude")
    if latitude is None or longitude is None:
        # dispersa las pulseras alrededor del punto por defecto (~1 km)
        return (DEFAULT_HOME_LATITUDE + random.uniform(-0.01, 0.01),
                DEFAULT_HOME_LONGITUDE + random.uniform(-0.01, 0.01))
    return float(latitude), float(longitude)


def parse_device(raw) -> Device:
    """Traduce un WearableDeviceResource del backend al modelo del simulador.

    Acepta tambien un id suelto, para poder probar sin backend.
    """
    if isinstance(raw, str):
        raw = {"deviceId": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"cannot read a device from {type(raw).__name__}")

    device_id = _first(raw, "deviceId", "id", "serialNumber", "serial_number")
    if device_id is None:
        raise ValueError("device is missing deviceId/id/serialNumber")

    care_recipient = _first(
        raw, "careRecipientProfileId", "care_recipient_profile_id",
        "fragileCitizenId", "careRecipientId", "personUnderCareId",
    )
    latitude, longitude = _home_coordinates(raw)
    return Device(
        device_id=str(device_id),
        # el backend deberia proveerlo; si no, uno estable para no romper el flujo
        care_recipient_profile_id=str(care_recipient or uuid.uuid4()),
        home_latitude=latitude,
        home_longitude=longitude,
    )


def parse_device_list(data) -> List[Device]:
    if isinstance(data, dict):
        data = _first(data, "content", "data", "devices", "items") or []
    if not isinstance(data, list):
        raise ValueError("expected a list of devices")
    return [parse_device(item) for item in data]


class SimulatorApi:
    """Expone los endpoints de control/monitoreo y controla el loop de emision."""

    def __init__(
        self,
        generator: SignalGenerator,
        publisher: MqttPublisher,
        backend_devices_url: str,
        emit_interval_seconds: int = 10,
    ):
        self._generator = generator
        self._publisher = publisher
        self._backend_devices_url = backend_devices_url
        self._emit_interval_seconds = emit_interval_seconds
        self._started_at = datetime.now(timezone.utc)
        self._last_update_at = None
        self._last_update_error = None
        self._cycles = 0
        self._loop_enabled = threading.Event()
        self._loop_enabled.set()
        self._app = Flask(__name__)
        self._register_routes()

    # ---------- dispositivos ----------

    def _fetch_devices(self) -> List[Device]:
        response = requests.get(self._backend_devices_url, timeout=5)
        response.raise_for_status()
        return parse_device_list(response.json())

    def _load_devices(self, devices: List[Device]) -> None:
        self._generator.sync_devices(devices)
        self._last_update_at = datetime.now(timezone.utc).isoformat()
        self._last_update_error = None

    def _devices_payload(self) -> dict:
        devices = self._generator.devices()
        return {"count": len(devices), "devices": [d.to_dict() for d in devices]}

    # ---------- rutas ----------

    def _register_routes(self):
        app = self._app

        @app.route("/health", methods=["GET"])
        def health():
            return jsonify({
                "status": "ok",
                "uptimeSeconds": self._uptime(),
                "mqttConnected": self._publisher.is_connected,
                "loopRunning": self._loop_enabled.is_set(),
                "deviceCount": len(self._generator.device_ids()),
            }), 200

        @app.route("/update", methods=["POST"])
        def update_devices():
            try:
                devices = self._fetch_devices()
            except (requests.RequestException, ValueError, KeyError, TypeError) as e:
                self._last_update_error = str(e)
                logger.error("Device update from %s failed: %s", self._backend_devices_url, e)
                return jsonify({"status": "error", "message": str(e)}), 500
            self._load_devices(devices)
            return jsonify({"status": "ok", **self._devices_payload()}), 200

        @app.route("/status", methods=["GET"])
        def status():
            return jsonify(self._devices_payload()), 200

        @app.route("/devices", methods=["PUT"])
        def set_devices():
            """Carga dispositivos a mano (util para probar sin el backend)."""
            body = request.get_json(silent=True) or {}
            raw = body.get("devices") or body.get("deviceIds")
            if not isinstance(raw, list) or not raw:
                return jsonify({
                    "status": "error",
                    "message": "body must carry a non-empty 'deviceIds' or 'devices' list",
                }), 400
            try:
                devices = [parse_device(item) for item in raw]
            except ValueError as e:
                return jsonify({"status": "error", "message": str(e)}), 400
            self._load_devices(devices)
            return jsonify({"status": "ok", **self._devices_payload()}), 200

        @app.route("/state", methods=["GET"])
        def state():
            """Estado vivo simulado de cada pulsera (bateria, posicion, episodios)."""
            return jsonify({"devices": self._generator.snapshot()}), 200

        @app.route("/stats", methods=["GET"])
        def stats():
            return jsonify({
                "uptimeSeconds": self._uptime(),
                "startedAt": self._started_at.isoformat(),
                "emitIntervalSeconds": self._emit_interval_seconds,
                "loopRunning": self._loop_enabled.is_set(),
                "cyclesEmitted": self._cycles,
                "scheduledSignals": self._generator.pending_count(),
                "backendDevicesUrl": self._backend_devices_url,
                "deviceCount": len(self._generator.device_ids()),
                "deviceIds": self._generator.device_ids(),
                "lastUpdateAt": self._last_update_at,
                "lastUpdateError": self._last_update_error,
                "mqtt": self._publisher.stats(),
            }), 200

        @app.route("/signals", methods=["GET"])
        def signals():
            try:
                limit = int(request.args.get("limit", 10))
            except ValueError:
                return jsonify({"status": "error", "message": "limit must be an integer"}), 400
            limit = max(1, min(limit, 200))
            channel = request.args.get("channel")
            if channel and channel not in CHANNELS:
                return jsonify({
                    "status": "error",
                    "message": f"channel must be one of {', '.join(CHANNELS)}",
                }), 400
            return jsonify({"signals": self._publisher.recent_signals(
                limit=limit,
                channel=channel,
                device_id=request.args.get("deviceId"),
                severity=request.args.get("severity"),
            )}), 200

        @app.route("/emit", methods=["POST"])
        def emit():
            """Fuerza la emision de un evento, sin esperar al loop."""
            body = request.get_json(silent=True) or {}
            device_id = body.get("deviceId")
            if device_id is not None and not isinstance(device_id, str):
                return jsonify({"status": "error", "message": "deviceId must be a string"}), 400
            event = body.get("event", "CYCLE")
            if not isinstance(event, str) or event.upper() not in TRIGGER_EVENTS:
                return jsonify({
                    "status": "error",
                    "message": f"event must be one of {', '.join(TRIGGER_EVENTS)}",
                }), 400
            try:
                records = self._emit(self._generator.trigger(
                    event=event,
                    device_id=device_id,
                    vital_sign_type=body.get("vitalSignType"),
                    direction=body.get("direction"),
                ))
            except ValueError as e:
                return jsonify({"status": "error", "message": str(e)}), 409
            accepted = all(r["accepted"] for r in records)
            return jsonify({
                "status": "ok" if accepted else "error",
                "count": len(records),
                "signals": records,
            }), 200

        @app.route("/loop", methods=["POST"])
        def loop_control():
            body = request.get_json(silent=True) or {}
            action = body.get("action")
            if action == "start":
                self._loop_enabled.set()
            elif action == "stop":
                self._loop_enabled.clear()
            else:
                return jsonify({"status": "error", "message": "action must be 'start' or 'stop'"}), 400
            return jsonify({"status": "ok", "loopRunning": self._loop_enabled.is_set()}), 200

        @app.route("/vital-sign-types", methods=["GET"])
        def vital_sign_types():
            from model.signal import VITAL_SIGN_SPECS
            return jsonify({"vitalSignTypes": [
                {
                    "code": s.code, "name": s.name, "unit": s.unit,
                    "normalRange": {"min": s.normal_min, "max": s.normal_max},
                    "criticalRange": {"min": s.critical_min, "max": s.critical_max},
                }
                for s in VITAL_SIGN_SPECS.values()
            ]}), 200

    # ---------- emision ----------

    def _uptime(self) -> float:
        return round((datetime.now(timezone.utc) - self._started_at).total_seconds(), 1)

    def _emit(self, signals) -> List[dict]:
        return self._publisher.publish_all(signals)

    def _emit_cycle(self) -> None:
        for device_id in self._generator.device_ids():
            self._emit(self._generator.generate_cycle(device_id))
        self._cycles += 1

    def _simulation_loop(self):
        """Emite un ciclo cada `emit_interval_seconds`, pero despierta cada segundo
        para despachar las señales diferidas (la ventana de cancelacion de caida
        es de 20s y no debe esperar al siguiente ciclo)."""
        next_cycle = time.monotonic()
        while True:
            try:
                now = time.monotonic()
                if now >= next_cycle:
                    if self._loop_enabled.is_set() and self._generator.device_ids():
                        self._emit_cycle()
                    next_cycle = now + self._emit_interval_seconds
                due = self._generator.due_signals()
                if due:
                    self._emit(due)
            except Exception as e:  # el loop nunca debe morir por una señal fallida
                logger.exception("Simulation loop error: %s", e)
            time.sleep(1)

    def run(self, host: str = "0.0.0.0", port: int = 5000):
        self._publisher.connect()
        try:
            self._load_devices(self._fetch_devices())
            logger.info("Loaded %s wearable device(s) from %s",
                        len(self._generator.device_ids()), self._backend_devices_url)
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            # el backend puede no estar arriba todavia; el CLI puede cargarlos a mano
            self._last_update_error = str(e)
            logger.warning("Could not load devices from %s: %s", self._backend_devices_url, e)
        threading.Thread(target=self._simulation_loop, daemon=True).start()
        logger.info("Simulator API listening on %s:%s", host, port)
        self._app.run(host=host, port=port)
