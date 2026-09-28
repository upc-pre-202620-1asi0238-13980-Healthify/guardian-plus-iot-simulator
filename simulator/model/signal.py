"""Modelo de las señales que emite la pulsera Guardian+.

A diferencia de un sensor de una sola señal, la pulsera emite varias familias de
telemetría y cada una viaja por un canal MQTT distinto, según el Bounded Context
que la consume:

    vitals    -> Health Monitoring        (DetectVitalSignsCommand)
    alerts    -> Emergency & Alerting     (caída, SOS, batería)
    location  -> Mobility & Geofencing    (WearableLocationMessage)
    activity  -> Care Routines & Wellness (ActivityTelemetryConsumer)
    sleep     -> Care Routines & Wellness (SleepTelemetryConsumer)
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Tuple

# ---------------- canales ----------------

CHANNEL_VITALS = "vitals"
CHANNEL_ALERTS = "alerts"
CHANNEL_LOCATION = "location"
CHANNEL_ACTIVITY = "activity"
CHANNEL_SLEEP = "sleep"

CHANNELS = (CHANNEL_VITALS, CHANNEL_ALERTS, CHANNEL_LOCATION, CHANNEL_ACTIVITY, CHANNEL_SLEEP)

# ---------------- estados clínicos ----------------

STATUS_NORMAL = "NORMAL"
STATUS_HIGH = "HIGH"
STATUS_LOW = "LOW"
STATUS_CRITICAL_HIGH = "CRITICAL_HIGH"
STATUS_CRITICAL_LOW = "CRITICAL_LOW"

OUT_OF_RANGE = (STATUS_HIGH, STATUS_LOW, STATUS_CRITICAL_HIGH, STATUS_CRITICAL_LOW)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class VitalSignSpec:
    """Catálogo de VitalSignType con los umbrales clínicos del reporte (US01-US05).

    `normal_*` delimita la banda fisiológica esperada; `critical_*` delimita el
    umbral de seguridad cuya transgresión sostenida debe disparar
    `VitalSignAnomalyDetectedIntegrationEvent` en el backend.
    """

    code: str
    name: str
    unit: str
    baseline: float
    normal_min: float
    normal_max: float
    critical_min: float
    critical_max: float
    drift: float
    decimals: int = 0
    anomaly_low_range: Optional[Tuple[float, float]] = None
    anomaly_high_range: Optional[Tuple[float, float]] = None

    def classify(self, value: float) -> str:
        if value < self.critical_min:
            return STATUS_CRITICAL_LOW
        if value > self.critical_max:
            return STATUS_CRITICAL_HIGH
        if value < self.normal_min:
            return STATUS_LOW
        if value > self.normal_max:
            return STATUS_HIGH
        return STATUS_NORMAL

    def round(self, value: float) -> float:
        return round(value, self.decimals) if self.decimals else float(round(value))


# Umbrales tomados de los criterios de aceptación del capítulo II del reporte.
VITAL_SIGN_SPECS = {
    "HR": VitalSignSpec(
        code="HR", name="Frecuencia cardiaca", unit="bpm",
        baseline=72, normal_min=60, normal_max=100,
        critical_min=50, critical_max=130, drift=4,
        anomaly_low_range=(38, 49), anomaly_high_range=(131, 168),
    ),
    "BP_SYS": VitalSignSpec(
        code="BP_SYS", name="Presion arterial sistolica", unit="mmHg",
        baseline=118, normal_min=90, normal_max=120,
        critical_min=90, critical_max=140, drift=6,
        anomaly_low_range=(74, 89), anomaly_high_range=(141, 186),
    ),
    "BP_DIA": VitalSignSpec(
        code="BP_DIA", name="Presion arterial diastolica", unit="mmHg",
        baseline=78, normal_min=60, normal_max=80,
        critical_min=60, critical_max=90, drift=4,
        anomaly_low_range=(44, 59), anomaly_high_range=(91, 116),
    ),
    "SPO2": VitalSignSpec(
        code="SPO2", name="Saturacion de oxigeno", unit="%",
        baseline=97, normal_min=95, normal_max=100,
        critical_min=90, critical_max=100.5, drift=1,
        anomaly_low_range=(79, 89),
    ),
    "TEMP": VitalSignSpec(
        code="TEMP", name="Temperatura corporal", unit="C",
        baseline=36.6, normal_min=36.0, normal_max=37.2,
        critical_min=35.0, critical_max=37.8, drift=0.15, decimals=1,
        anomaly_low_range=(33.4, 34.9), anomaly_high_range=(37.9, 40.2),
    ),
    "RESP_RATE": VitalSignSpec(
        code="RESP_RATE", name="Frecuencia respiratoria", unit="rpm",
        baseline=16, normal_min=12, normal_max=20,
        critical_min=10, critical_max=24, drift=1.2,
        anomaly_low_range=(6, 9), anomaly_high_range=(25, 35),
    ),
}

VITAL_SIGN_CODES = tuple(VITAL_SIGN_SPECS)


@dataclass
class Device:
    """Pulsera asignada a un Fragile Citizen.

    `care_recipient_profile_id` se arrastra en cada señal porque todos los
    contextos consumidores referencian al paciente, no al hardware.
    """

    device_id: str
    care_recipient_profile_id: str
    home_latitude: float
    home_longitude: float

    def to_dict(self) -> dict:
        return {
            "deviceId": self.device_id,
            "careRecipientProfileId": self.care_recipient_profile_id,
            "homeLatitude": round(self.home_latitude, 6),
            "homeLongitude": round(self.home_longitude, 6),
        }


@dataclass
class Signal:
    """Una señal emitida por la pulsera, lista para publicarse en el broker."""

    device_id: str
    care_recipient_profile_id: str
    channel: str
    signal_type: str
    payload: dict
    timestamp: str
    severity: str = "INFO"

    @staticmethod
    def create(device: Device, channel: str, signal_type: str,
               payload: dict, severity: str = "INFO") -> "Signal":
        return Signal(
            device_id=device.device_id,
            care_recipient_profile_id=device.care_recipient_profile_id,
            channel=channel,
            signal_type=signal_type,
            payload=payload,
            timestamp=now_iso(),
            severity=severity,
        )

    def to_dict(self) -> dict:
        """Payload publicado al broker (camelCase, como lo espera el backend)."""
        return {
            "deviceId": self.device_id,
            "careRecipientProfileId": self.care_recipient_profile_id,
            "signalType": self.signal_type,
            "severity": self.severity,
            "measuredAt": self.timestamp,
            **self.payload,
        }

    def summary(self) -> str:
        """Resumen de una linea para el CLI."""
        p = self.payload
        if self.channel == CHANNEL_VITALS:
            return f"{p['vitalSignTypeCode']} {p['value']} {p['unit']} ({p['status']})"
        if self.channel == CHANNEL_LOCATION:
            zone = "outside" if p.get("outsideSafeZone") else "inside"
            return f"{p['latitude']:.5f},{p['longitude']:.5f} +-{p['accuracyInMeters']}m ({zone})"
        if self.channel == CHANNEL_ACTIVITY:
            return f"inactive {p['inactiveMinutes']}min, steps {p.get('steps', 0)}"
        if self.channel == CHANNEL_SLEEP:
            return f"{p['sleepHours']}h, {p['interruptions']} wakeups ({p['classification']})"
        if self.signal_type == "FALL_DETECTED":
            return f"impact {p['impactG']}g, {p['cancelWindowSeconds']}s to cancel"
        if self.signal_type == "FALL_CONFIRMED":
            return f"escalated after {p['elapsedSeconds']}s, GPS sent"
        if self.signal_type == "FALL_CANCELLED":
            return f"false positive after {p['elapsedSeconds']}s"
        if self.signal_type == "SOS_TRIGGERED":
            return f"held {p.get('pressDurationMs', 0)}ms"
        if self.signal_type.startswith("BATTERY"):
            return f"battery {p.get('batteryLevel')}%"
        return ""


@dataclass
class SimulationConfig:
    """Parametros de simulacion; los defaults salen de los criterios del reporte."""

    # cada tick del loop representa este tiempo simulado, para que los contadores
    # de inactividad y la descarga de bateria avancen a una velocidad observable
    minutes_per_tick: float = 5.0

    # regla de tolerancia: lecturas consecutivas fuera de umbral que el backend
    # exige antes de confirmar la anomalia (reporte: mas de 3)
    required_consecutive_hits: int = 4

    # probabilidades por dispositivo y por tick
    anomaly_chance: float = 0.05
    fall_chance: float = 0.004
    sos_chance: float = 0.003
    false_alarm_chance: float = 0.35     # caidas canceladas por el usuario
    wander_chance: float = 0.02          # salida de la zona segura
    movement_chance: float = 0.55        # el paciente se mueve en este tick

    # caida: ventana local de cancelacion antes de escalar (reporte: 20s)
    fall_cancel_window_seconds: int = 20

    # inactividad prolongada (reporte: mas de 60 min en jornada activa)
    inactivity_threshold_minutes: int = 60

    # bateria
    battery_drain_per_tick: float = 0.35
    battery_low_threshold: int = 20
    battery_critical_threshold: int = 5

    # zona segura y GPS
    safe_zone_radius_meters: float = 200.0
    gps_accuracy_range: Tuple[float, float] = (3.0, 18.0)

    # sueño: cada cuantos ticks se consolida un reporte nocturno
    sleep_report_every_ticks: int = 60
    max_sleep_interruptions: int = 4     # mas de 4 -> sueño fragmentado
