"""Generacion de las señales de la pulsera Guardian+.

El simulador de spottrack era sin estado: cada señal se sorteaba de cero. Aqui el
generador mantiene estado por dispositivo (bateria, posicion, contador de
inactividad, episodios clinicos en curso) para que la telemetria sea coherente
en el tiempo: la bateria baja, el paciente camina y vuelve, y una anomalia dura
varias lecturas consecutivas, que es lo que el motor de reglas del backend
necesita para aplicar su Regla de Tolerancia.
"""

import math
import random
import threading
import time
import uuid
from typing import Dict, List, Optional

from model.signal import (
    CHANNEL_ACTIVITY,
    CHANNEL_ALERTS,
    CHANNEL_LOCATION,
    CHANNEL_SLEEP,
    CHANNEL_VITALS,
    OUT_OF_RANGE,
    VITAL_SIGN_CODES,
    VITAL_SIGN_SPECS,
    Device,
    Signal,
    SimulationConfig,
    now_iso,
)

EARTH_METERS_PER_DEGREE = 111320.0


def _offset_meters(lat: float, lon: float, north_m: float, east_m: float):
    d_lat = north_m / EARTH_METERS_PER_DEGREE
    d_lon = east_m / (EARTH_METERS_PER_DEGREE * math.cos(math.radians(lat)) or 1e-9)
    return lat + d_lat, lon + d_lon


def _distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    d_north = (lat2 - lat1) * EARTH_METERS_PER_DEGREE
    d_east = (lon2 - lon1) * EARTH_METERS_PER_DEGREE * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(d_north, d_east)


class DeviceState:
    """Estado vivo de una pulsera entre ticks de simulacion."""

    def __init__(self, device: Device, config: SimulationConfig):
        self.device = device
        self.battery_level = random.uniform(55.0, 100.0)
        self.latitude = device.home_latitude
        self.longitude = device.home_longitude
        self.inactive_minutes = 0.0
        self.steps = 0
        self.inactivity_reported = False
        self.battery_low_reported = False
        self.battery_critical_reported = False
        self.episodes: Dict[str, dict] = {}
        self.ticks = 0
        self.ticks_since_sleep_report = random.randint(0, config.sleep_report_every_ticks - 1)
        self.baselines = {
            code: spec.baseline + random.uniform(-spec.drift, spec.drift)
            for code, spec in VITAL_SIGN_SPECS.items()
        }

    def snapshot(self, config: SimulationConfig) -> dict:
        return {
            **self.device.to_dict(),
            "batteryLevel": round(self.battery_level, 1),
            "latitude": round(self.latitude, 6),
            "longitude": round(self.longitude, 6),
            "distanceFromHomeMeters": round(_distance_meters(
                self.device.home_latitude, self.device.home_longitude,
                self.latitude, self.longitude), 1),
            "outsideSafeZone": self.is_outside_safe_zone(config),
            "inactiveMinutes": round(self.inactive_minutes, 1),
            "steps": self.steps,
            "activeEpisodes": [
                {"vitalSignTypeCode": code, "direction": ep["direction"], "readingsLeft": ep["remaining"]}
                for code, ep in self.episodes.items()
            ],
        }

    def is_outside_safe_zone(self, config: SimulationConfig) -> bool:
        distance = _distance_meters(
            self.device.home_latitude, self.device.home_longitude,
            self.latitude, self.longitude,
        )
        return distance > config.safe_zone_radius_meters


class SignalGenerator:
    """Produce las señales de cada pulsera y agenda las de despacho diferido."""

    def __init__(self, config: SimulationConfig):
        self._config = config
        self._lock = threading.Lock()
        self._states: Dict[str, DeviceState] = {}
        self._order: List[str] = []
        self._scheduled: List[dict] = []

    # ---------------- dispositivos ----------------

    def sync_devices(self, devices: List[Device]) -> None:
        """Alinea el estado interno con la lista de dispositivos del backend."""
        with self._lock:
            incoming = {d.device_id: d for d in devices}
            for device_id, device in incoming.items():
                state = self._states.get(device_id)
                if state is None:
                    self._states[device_id] = DeviceState(device, self._config)
                else:
                    state.device = device
            for device_id in list(self._states):
                if device_id not in incoming:
                    del self._states[device_id]
            self._order = [d.device_id for d in devices]
            self._scheduled = [s for s in self._scheduled if s["deviceId"] in incoming]

    def device_ids(self) -> List[str]:
        with self._lock:
            return list(self._order)

    def devices(self) -> List[Device]:
        with self._lock:
            return [self._states[d].device for d in self._order if d in self._states]

    def snapshot(self) -> List[dict]:
        with self._lock:
            return [self._states[d].snapshot(self._config) for d in self._order if d in self._states]

    def _resolve(self, device_id: Optional[str]) -> DeviceState:
        if not self._states:
            raise ValueError("No wearable devices loaded to generate signals")
        if device_id is None:
            device_id = random.choice(list(self._states))
        state = self._states.get(device_id)
        if state is None:
            raise ValueError(f"Unknown device {device_id!r}")
        return state

    # ---------------- ciclo completo ----------------

    def generate_cycle(self, device_id: Optional[str] = None) -> List[Signal]:
        """Un tick de simulacion: todas las señales que la pulsera emitiria ahora."""
        with self._lock:
            state = self._resolve(device_id)
            state.ticks += 1
            signals: List[Signal] = []
            signals.extend(self._vitals(state))
            signals.extend(self._movement_and_location(state))
            signals.extend(self._battery(state))
            signals.extend(self._sleep(state))
            signals.extend(self._incidents(state))
            return signals

    # ---------------- signos vitales ----------------

    def _vitals(self, state: DeviceState) -> List[Signal]:
        if random.random() < self._config.anomaly_chance:
            self._start_episode(state)
        return [self._vital_reading(state, code) for code in VITAL_SIGN_CODES]

    def _start_episode(self, state: DeviceState, code: Optional[str] = None,
                       direction: Optional[str] = None) -> Optional[dict]:
        """Abre un episodio clinico sostenido sobre un signo vital.

        Dura `required_consecutive_hits` lecturas para que el backend confirme la
        anomalia en lugar de descartarla como lectura aislada.
        """
        if code is None:
            free = [c for c in VITAL_SIGN_CODES if c not in state.episodes]
            if not free:
                return None
            code = random.choice(free)
        spec = VITAL_SIGN_SPECS[code]
        options = [d for d, r in (("LOW", spec.anomaly_low_range), ("HIGH", spec.anomaly_high_range)) if r]
        if not options:
            return None
        episode = {
            "direction": direction if direction in options else random.choice(options),
            "remaining": self._config.required_consecutive_hits,
        }
        state.episodes[code] = episode
        return episode

    def _vital_reading(self, state: DeviceState, code: str) -> Signal:
        spec = VITAL_SIGN_SPECS[code]
        episode = state.episodes.get(code)

        if episode is not None:
            low, high = (spec.anomaly_low_range if episode["direction"] == "LOW"
                         else spec.anomaly_high_range)
            value = random.uniform(low, high)
            episode["remaining"] -= 1
            if episode["remaining"] <= 0:
                del state.episodes[code]
        else:
            # deriva lenta alrededor del basal propio del paciente
            baseline = state.baselines[code]
            baseline += random.gauss(0, spec.drift / 3)
            baseline = min(max(baseline, spec.normal_min + spec.drift / 2),
                           spec.normal_max - spec.drift / 2)
            state.baselines[code] = baseline
            value = random.gauss(baseline, spec.drift / 2)
            value = min(max(value, spec.normal_min), spec.normal_max)

        value = spec.round(value)
        status = spec.classify(value)
        return Signal.create(
            state.device, CHANNEL_VITALS, "VITAL_SIGN_READING",
            {
                "vitalSignTypeCode": spec.code,
                "vitalSignTypeName": spec.name,
                "value": value,
                "unit": spec.unit,
                "status": status,
                "normalRange": {"min": spec.normal_min, "max": spec.normal_max},
                "sustained": episode is not None,
            },
            severity="WARNING" if status in OUT_OF_RANGE else "INFO",
        )

    # ---------------- movimiento, ubicacion y actividad ----------------

    def _movement_and_location(self, state: DeviceState) -> List[Signal]:
        config = self._config
        moving = random.random() < config.movement_chance
        signals: List[Signal] = []
        steps = 0

        if moving:
            wandering = random.random() < config.wander_chance
            distance = random.uniform(60, 220) if wandering else random.uniform(4, 45)
            bearing = random.uniform(0, 2 * math.pi)
            if not wandering and state.is_outside_safe_zone(config):
                # sin intencion de alejarse, el paciente tiende a volver a casa
                bearing = math.atan2(
                    state.device.home_longitude - state.longitude,
                    state.device.home_latitude - state.latitude,
                )
            state.latitude, state.longitude = _offset_meters(
                state.latitude, state.longitude,
                distance * math.cos(bearing), distance * math.sin(bearing),
            )
            steps = int(distance * random.uniform(1.1, 1.6))
            state.steps += steps

        signals.append(self._location_signal(state, moving, steps))
        signals.extend(self._activity(state, moving, steps))
        return signals

    def _location_signal(self, state: DeviceState, moving: bool, steps: int) -> Signal:
        config = self._config
        outside = state.is_outside_safe_zone(config)
        distance = _distance_meters(
            state.device.home_latitude, state.device.home_longitude,
            state.latitude, state.longitude,
        )
        return Signal.create(
            state.device, CHANNEL_LOCATION, "LOCATION_UPDATE",
            {
                # contrato WearableLocationMessage del Bounded Context Mobility
                "latitude": round(state.latitude, 6),
                "longitude": round(state.longitude, 6),
                "accuracyInMeters": round(random.uniform(*config.gps_accuracy_range), 1),
                "recordedAt": now_iso(),
                "speedMps": round(steps / (config.minutes_per_tick * 60) * 0.75, 2) if moving else 0.0,
                "distanceFromHomeMeters": round(distance, 1),
                "outsideSafeZone": outside,
            },
            severity="WARNING" if outside else "INFO",
        )

    def _activity(self, state: DeviceState, moving: bool, steps: int) -> List[Signal]:
        config = self._config
        signals: List[Signal] = []

        if moving:
            resumed_after = state.inactive_minutes
            state.inactive_minutes = 0.0
            if resumed_after >= config.minutes_per_tick:
                # RecordActivityResumedCommand: reinicia el contador sin alarma
                state.inactivity_reported = False
                signals.append(Signal.create(
                    state.device, CHANNEL_ACTIVITY, "ACTIVITY_RESUMED",
                    {
                        "inactiveMinutes": round(resumed_after, 1),
                        "thresholdMinutes": config.inactivity_threshold_minutes,
                        "steps": steps,
                        "movementIntensity": round(min(steps / 120.0, 1.0), 2),
                    },
                ))
        else:
            state.inactive_minutes += config.minutes_per_tick

        signals.append(Signal.create(
            state.device, CHANNEL_ACTIVITY, "ACTIVITY_SAMPLE",
            {
                "inactiveMinutes": round(state.inactive_minutes, 1),
                "thresholdMinutes": config.inactivity_threshold_minutes,
                "steps": steps,
                "stepsAccumulated": state.steps,
                "movementIntensity": round(min(steps / 120.0, 1.0), 2),
            },
        ))

        if state.inactive_minutes > config.inactivity_threshold_minutes and not state.inactivity_reported:
            # RecordProlongedInactivityCommand -> ProlongedInactivityDetectedIntegrationEvent
            state.inactivity_reported = True
            signals.append(Signal.create(
                state.device, CHANNEL_ACTIVITY, "PROLONGED_INACTIVITY",
                {
                    "inactiveMinutes": round(state.inactive_minutes, 1),
                    "thresholdMinutes": config.inactivity_threshold_minutes,
                    "steps": 0,
                },
                severity="WARNING",
            ))
        return signals

    # ---------------- bateria ----------------

    def _battery(self, state: DeviceState) -> List[Signal]:
        config = self._config
        state.battery_level = max(0.0, state.battery_level - config.battery_drain_per_tick)
        level = round(state.battery_level, 1)
        signals: List[Signal] = []

        if level <= config.battery_critical_threshold and not state.battery_critical_reported:
            state.battery_critical_reported = True
            signals.append(Signal.create(
                state.device, CHANNEL_ALERTS, "BATTERY_CRITICAL",
                {"batteryLevel": level, "thresholdPercent": config.battery_critical_threshold,
                 "monitoringSuspensionImminent": True},
                severity="CRITICAL",
            ))
        elif level <= config.battery_low_threshold and not state.battery_low_reported:
            state.battery_low_reported = True
            signals.append(Signal.create(
                state.device, CHANNEL_ALERTS, "BATTERY_LOW",
                {"batteryLevel": level, "thresholdPercent": config.battery_low_threshold,
                 "monitoringSuspensionImminent": False},
                severity="WARNING",
            ))

        if state.battery_level <= 0.0:
            # el cuidador pone la pulsera a cargar
            state.battery_level = 100.0
            state.battery_low_reported = False
            state.battery_critical_reported = False
        return signals

    # ---------------- sueño ----------------

    def _sleep(self, state: DeviceState) -> List[Signal]:
        config = self._config
        state.ticks_since_sleep_report += 1
        if state.ticks_since_sleep_report < config.sleep_report_every_ticks:
            return []
        state.ticks_since_sleep_report = 0
        return [self._sleep_signal(state)]

    def _sleep_signal(self, state: DeviceState) -> Signal:
        config = self._config
        hours = round(random.uniform(4.0, 9.0), 1)
        interruptions = random.choices(range(0, 8), weights=[18, 22, 20, 15, 10, 7, 5, 3])[0]
        fragmented = interruptions > config.max_sleep_interruptions
        return Signal.create(
            state.device, CHANNEL_SLEEP, "SLEEP_CYCLE_RECORDED",
            {
                "sleepHours": hours,
                "interruptions": interruptions,
                "continuityIndex": round(max(0.0, 1 - interruptions / 8) * min(hours / 8, 1.0), 2),
                "classification": "FRAGMENTED" if fragmented else "RESTFUL",
                "maxInterruptions": config.max_sleep_interruptions,
            },
            severity="WARNING" if fragmented else "INFO",
        )

    # ---------------- incidentes ----------------

    def _incidents(self, state: DeviceState) -> List[Signal]:
        signals: List[Signal] = []
        if random.random() < self._config.fall_chance:
            signals.append(self._fall_detected(state))
        if random.random() < self._config.sos_chance:
            signals.append(self._sos(state))
        return signals

    def _fall_detected(self, state: DeviceState) -> Signal:
        """Caida detectada: abre la ventana local de cancelacion y agenda el desenlace."""
        config = self._config
        fall_event_id = str(uuid.uuid4())
        cancelled = random.random() < config.false_alarm_chance
        elapsed = (round(random.uniform(3, config.fall_cancel_window_seconds - 2), 1)
                   if cancelled else config.fall_cancel_window_seconds)

        self._scheduled.append({
            "deviceId": state.device.device_id,
            "dueAt": time.monotonic() + elapsed,
            "build": (lambda: self._fall_outcome(state, fall_event_id, cancelled, elapsed)),
        })

        return Signal.create(
            state.device, CHANNEL_ALERTS, "FALL_DETECTED",
            {
                "fallEventId": fall_event_id,
                "impactG": round(random.uniform(2.4, 6.8), 2),
                "freeFallMs": random.randint(180, 620),
                "immobilitySeconds": random.randint(3, 12),
                "cancelWindowSeconds": config.fall_cancel_window_seconds,
                "latitude": round(state.latitude, 6),
                "longitude": round(state.longitude, 6),
            },
            severity="CRITICAL",
        )

    def _fall_outcome(self, state: DeviceState, fall_event_id: str,
                      cancelled: bool, elapsed: float) -> Signal:
        if cancelled:
            # el Fragile Citizen confirmo su bienestar dentro de la ventana
            return Signal.create(
                state.device, CHANNEL_ALERTS, "FALL_CANCELLED",
                {
                    "fallEventId": fall_event_id,
                    "reason": "USER_CONFIRMED_WELLBEING",
                    "elapsedSeconds": elapsed,
                    "resolution": "FALSE_POSITIVE_RESOLVED",
                },
            )
        # venció el temporizador sin interaccion: se escala con coordenadas GPS
        return Signal.create(
            state.device, CHANNEL_ALERTS, "FALL_CONFIRMED",
            {
                "fallEventId": fall_event_id,
                "elapsedSeconds": elapsed,
                "latitude": round(state.latitude, 6),
                "longitude": round(state.longitude, 6),
                "accuracyInMeters": round(random.uniform(*self._config.gps_accuracy_range), 1),
                "resolution": "ESCALATED_NO_RESPONSE",
            },
            severity="CRITICAL",
        )

    def _sos(self, state: DeviceState) -> Signal:
        return Signal.create(
            state.device, CHANNEL_ALERTS, "SOS_TRIGGERED",
            {
                "sosEventId": str(uuid.uuid4()),
                "pressDurationMs": random.randint(900, 3200),
                "latitude": round(state.latitude, 6),
                "longitude": round(state.longitude, 6),
                "accuracyInMeters": round(random.uniform(*self._config.gps_accuracy_range), 1),
            },
            severity="CRITICAL",
        )

    # ---------------- despacho diferido ----------------

    def due_signals(self) -> List[Signal]:
        """Señales cuyo temporizador ya vencio (desenlace de una caida)."""
        now = time.monotonic()
        with self._lock:
            due = [s for s in self._scheduled if s["dueAt"] <= now]
            if not due:
                return []
            self._scheduled = [s for s in self._scheduled if s["dueAt"] > now]
            return [entry["build"]() for entry in due]

    def pending_count(self) -> int:
        with self._lock:
            return len(self._scheduled)

    # ---------------- disparo manual ----------------

    def trigger(self, event: str, device_id: Optional[str] = None,
                vital_sign_type: Optional[str] = None,
                direction: Optional[str] = None) -> List[Signal]:
        """Fuerza un evento concreto, sin esperar al azar del loop."""
        event = (event or "CYCLE").upper()
        if event == "CYCLE":
            return self.generate_cycle(device_id)

        with self._lock:
            state = self._resolve(device_id)
            if event == "FALL":
                return [self._fall_detected(state)]
            if event == "SOS":
                return [self._sos(state)]
            if event == "SLEEP":
                return [self._sleep_signal(state)]
            if event == "LOCATION":
                return [self._location_signal(state, moving=False, steps=0)]
            if event == "ACTIVITY":
                return self._activity(state, moving=False, steps=0)
            if event == "BATTERY":
                state.battery_level = min(state.battery_level, self._config.battery_low_threshold)
                state.battery_low_reported = False
                return self._battery(state)
            if event == "VITALS":
                return [self._vital_reading(state, code) for code in VITAL_SIGN_CODES]
            if event == "ANOMALY":
                code = (vital_sign_type or "").upper() or None
                if code is not None and code not in VITAL_SIGN_SPECS:
                    raise ValueError(
                        f"unknown vitalSignType {code!r}; expected one of {', '.join(VITAL_SIGN_CODES)}")
                if code is not None:
                    state.episodes.pop(code, None)
                episode = self._start_episode(state, code=code, direction=direction)
                if episode is None:
                    raise ValueError(f"{code} has no anomaly range configured")
                target = code or next(c for c, ep in state.episodes.items() if ep is episode)
                return [self._vital_reading(state, target)]
            raise ValueError(
                "event must be one of CYCLE, VITALS, ANOMALY, FALL, SOS, LOCATION, ACTIVITY, SLEEP, BATTERY")
