"""CLI del simulador Guardian+: levanta el simulador y permite inspeccionar como
va, incluyendo si el publisher MQTT realmente envio la telemetria al broker."""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

DEFAULT_URL = os.environ.get("SIMULATOR_URL", "http://localhost:5000")
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
BACKEND_DEVICES_URL = os.environ.get(
    "BACKEND_DEVICES_URL", "http://localhost:8080/api/v1/wearable-devices")
TOPIC_PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "guardian")

CHANNELS = ("vitals", "alerts", "location", "activity", "sleep")
EVENTS = ("cycle", "vitals", "anomaly", "fall", "sos", "location", "activity", "sleep", "battery")
VITAL_SIGN_CODES = ("HR", "BP_SYS", "BP_DIA", "SPO2", "TEMP", "RESP_RATE")

GREEN, RED, YELLOW, CYAN, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[0m")


def _color(text, code):
    return text if os.environ.get("NO_COLOR") else f"{code}{text}{RESET}"


def _ok(flag):
    return _color("yes", GREEN) if flag else _color("no", RED)


def _ago(iso_ts):
    """Segundos transcurridos desde un timestamp ISO, como texto."""
    if not iso_ts:
        return "never"
    try:
        delta = (datetime.now(timezone.utc) - datetime.fromisoformat(iso_ts)).total_seconds()
    except ValueError:
        return iso_ts
    if delta < 60:
        return f"{delta:.0f}s ago"
    if delta < 3600:
        return f"{delta / 60:.0f}m ago"
    return f"{delta / 3600:.1f}h ago"


def _severity_color(severity):
    return {"CRITICAL": RED, "WARNING": YELLOW}.get(severity, DIM)


# ---------------- HTTP helpers ----------------

def _request(args, method, path, **kwargs):
    url = args.url.rstrip("/") + path
    try:
        response = requests.request(method, url, timeout=10, **kwargs)
    except requests.RequestException as e:
        print(_color(f"Cannot reach the simulator at {args.url}: {e}", RED), file=sys.stderr)
        print(_color("Is it running? Start it with: python simulator/cli.py serve", DIM), file=sys.stderr)
        raise SystemExit(2)
    try:
        body = response.json()
    except ValueError:
        print(_color(f"HTTP {response.status_code}: {response.text[:200]}", RED), file=sys.stderr)
        raise SystemExit(1)
    return response.status_code, body


def _emit_json(body):
    print(json.dumps(body, indent=2))


# ---------------- rendering ----------------

def _render_stats(stats):
    mqtt_stats = stats["mqtt"]
    by_channel = mqtt_stats["byChannel"]
    lines = [
        "Simulator",
        f"  uptime            {stats['uptimeSeconds']}s",
        f"  emission loop     {_ok(stats['loopRunning'])} (cycle every {stats['emitIntervalSeconds']}s)",
        f"  cycles emitted    {stats['cyclesEmitted']}",
        f"  devices loaded    {stats['deviceCount']}",
        f"  scheduled signals {stats['scheduledSignals']} (fall outcomes awaiting their window)",
        f"  last device sync  {_ago(stats['lastUpdateAt'])}",
        f"  backend           {stats['backendDevicesUrl']}",
        "",
        "MQTT publisher",
        f"  broker            {mqtt_stats['host']}:{mqtt_stats['port']} (prefix {mqtt_stats['topicPrefix']}/)",
        f"  connected         {_ok(mqtt_stats['connected'])}",
        f"  signals sent      {mqtt_stats['published']}",
        f"  confirmed by lib  {mqtt_stats['delivered']}",
        f"  failed            {mqtt_stats['failed']}",
        f"  awaiting confirm  {mqtt_stats['pending']}",
        f"  critical alerts   {_color(str(mqtt_stats['criticalAlerts']), RED if mqtt_stats['criticalAlerts'] else DIM)}",
        f"  last signal       {_ago(mqtt_stats['lastPublishAt'])}",
    ]
    rate = mqtt_stats.get("rateLimit") or {}
    if rate.get("maxPerSecond"):
        lines.append(f"  rate limit        {rate['maxPerSecond']}/s burst {rate['burst']}, "
                     f"throttled {rate['throttled']} ({rate['throttledSeconds']}s)")
    lines += [
        "",
        "By channel        " + "  ".join(f"{c}={by_channel.get(c, 0)}" for c in CHANNELS),
    ]
    if stats.get("lastUpdateError"):
        lines.append(_color(f"  device sync error {stats['lastUpdateError']}", YELLOW))
    if mqtt_stats.get("lastError"):
        lines.append(_color(f"  last mqtt error   {mqtt_stats['lastError']}", YELLOW))
    print("\n".join(lines))


def _render_signals(signals):
    if not signals:
        print(_color("No signals published yet.", DIM))
        return
    print(f"{'WHEN':<9} {'DEVICE':<16} {'CHANNEL':<9} {'SIGNAL':<22} {'DETAIL':<34} {'SENT':<5} {'ACK':<5}")
    for s in signals:
        sent = "ok" if s["accepted"] else "FAIL"
        ack = "ok" if s["delivered"] else "..."
        when = s["timestamp"][11:19]
        signal_type = _color(f"{s['signalType']:<22}", _severity_color(s["severity"]))
        print(
            f"{when:<9} {s['deviceId'][:16]:<16} {s['channel']:<9} {signal_type} "
            f"{s['summary'][:34]:<34} "
            f"{_color(f'{sent:<5}', GREEN if s['accepted'] else RED)} "
            f"{_color(f'{ack:<5}', GREEN if s['delivered'] else YELLOW)}"
        )
        if s.get("error"):
            print(_color(f"    error: {s['error']}", RED))


def _render_state(devices):
    if not devices:
        print(_color("No devices loaded.", DIM))
        return
    print(f"{'DEVICE':<16} {'BATTERY':>8} {'INACTIVE':>9} {'STEPS':>7} {'FROM HOME':>10} {'ZONE':<8} EPISODES")
    for d in devices:
        battery = d["batteryLevel"]
        battery_cell = _color(f"{battery:>7.1f}%", RED if battery <= 5 else YELLOW if battery <= 20 else GREEN)
        inactive = d["inactiveMinutes"]
        inactive_cell = _color(f"{inactive:>8.0f}m", YELLOW if inactive > 60 else DIM)
        zone = "OUTSIDE" if d["outsideSafeZone"] else "inside"
        zone_cell = _color(f"{zone:<8}", RED if d["outsideSafeZone"] else DIM)
        episodes = ", ".join(
            f"{e['vitalSignTypeCode']} {e['direction']} x{e['readingsLeft']}"
            for e in d["activeEpisodes"]
        ) or _color("-", DIM)
        print(f"{d['deviceId'][:16]:<16} {battery_cell} {inactive_cell} {d['steps']:>7} "
              f"{d['distanceFromHomeMeters']:>9.0f}m {zone_cell} {episodes}")


# ---------------- commands ----------------

def cmd_serve(args):
    import logging

    from interfaces.api import SimulatorApi
    from interfaces.mqtt_publisher import MqttPublisher
    from model.signal import SimulationConfig
    from model.signal_generator import SignalGenerator

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    config = SimulationConfig(
        minutes_per_tick=args.minutes_per_tick,
        required_consecutive_hits=args.consecutive_hits,
        anomaly_chance=args.anomaly_chance,
        fall_chance=args.fall_chance,
        sos_chance=args.sos_chance,
        inactivity_threshold_minutes=args.inactivity_minutes,
        safe_zone_radius_meters=args.safe_zone_radius,
        battery_drain_per_tick=args.battery_drain,
    )
    generator = SignalGenerator(config)
    publisher = MqttPublisher(
        host=args.mqtt_host, port=args.mqtt_port, topic_prefix=args.topic_prefix,
        alerts_qos=args.alerts_qos, telemetry_qos=args.telemetry_qos,
        max_rate=args.max_rate, burst=args.burst, retain=args.retain,
    )
    api = SimulatorApi(
        generator=generator,
        publisher=publisher,
        backend_devices_url=args.backend_url,
        emit_interval_seconds=args.interval,
    )
    api.run(host=args.host, port=args.port)


def cmd_health(args):
    _, body = _request(args, "GET", "/health")
    if args.json:
        return _emit_json(body)
    print(f"simulator      {_ok(body['status'] == 'ok')}")
    print(f"mqtt connected {_ok(body['mqttConnected'])}")
    print(f"loop running   {_ok(body['loopRunning'])}")
    print(f"devices        {body['deviceCount']}")
    print(f"uptime         {body['uptimeSeconds']}s")


def cmd_status(args):
    _, body = _request(args, "GET", "/status")
    if args.json:
        return _emit_json(body)
    print(f"{body['count']} wearable device(s) loaded")
    for device in body["devices"]:
        print(f"  - {device['deviceId']}  care recipient {device['careRecipientProfileId']}  "
              f"home {device['homeLatitude']},{device['homeLongitude']}")


def cmd_state(args):
    _, body = _request(args, "GET", "/state")
    if args.json:
        return _emit_json(body)
    _render_state(body["devices"])


def cmd_stats(args):
    _, body = _request(args, "GET", "/stats")
    if args.json:
        return _emit_json(body)
    _render_stats(body)


def cmd_signals(args):
    params = {"limit": args.limit}
    if args.channel:
        params["channel"] = args.channel
    if args.device_id:
        params["deviceId"] = args.device_id
    if args.critical:
        params["severity"] = "CRITICAL"
    code, body = _request(args, "GET", "/signals", params=params)
    if args.json:
        return _emit_json(body)
    if code != 200:
        print(_color(f"Failed: {body.get('message')}", RED))
        raise SystemExit(1)
    _render_signals(body["signals"])


def cmd_update(args):
    code, body = _request(args, "POST", "/update")
    if args.json:
        return _emit_json(body)
    if code == 200:
        print(_color(f"Loaded {body['count']} wearable device(s) from the backend.", GREEN))
        for device in body["devices"]:
            print(f"  - {device['deviceId']}")
    else:
        print(_color(f"Update failed: {body.get('message')}", RED))
        raise SystemExit(1)


def cmd_devices(args):
    code, body = _request(args, "PUT", "/devices", json={"deviceIds": args.device_ids})
    if args.json:
        return _emit_json(body)
    if code == 200:
        print(_color(f"{body['count']} wearable device(s) set manually.", GREEN))
        for device in body["devices"]:
            print(f"  - {device['deviceId']}  care recipient {device['careRecipientProfileId']}")
    else:
        print(_color(f"Failed: {body.get('message')}", RED))
        raise SystemExit(1)


def cmd_emit(args):
    payload = {"event": args.event.upper()}
    if args.device_id:
        payload["deviceId"] = args.device_id
    if args.vital_sign_type:
        payload["vitalSignType"] = args.vital_sign_type
    if args.direction:
        payload["direction"] = args.direction.upper()
    code, body = _request(args, "POST", "/emit", json=payload)
    if args.json:
        return _emit_json(body)
    if code != 200:
        print(_color(f"Emit failed: {body.get('message')}", RED))
        raise SystemExit(1)
    _render_signals(body["signals"])


def cmd_loop(args):
    code, body = _request(args, "POST", "/loop", json={"action": args.action})
    if args.json:
        return _emit_json(body)
    if code != 200:
        print(_color(f"Failed: {body.get('message')}", RED))
        raise SystemExit(1)
    print(f"loop running   {_ok(body['loopRunning'])}")


def cmd_types(args):
    _, body = _request(args, "GET", "/vital-sign-types")
    if args.json:
        return _emit_json(body)
    print(f"{'CODE':<10} {'UNIT':<6} {'NORMAL':<16} {'CRITICAL OUTSIDE':<18} NAME")
    for t in body["vitalSignTypes"]:
        normal = f"{t['normalRange']['min']}-{t['normalRange']['max']}"
        critical = f"<{t['criticalRange']['min']} or >{t['criticalRange']['max']}"
        print(f"{t['code']:<10} {t['unit']:<6} {normal:<16} {critical:<18} {t['name']}")


def cmd_watch(args):
    try:
        while True:
            _, stats = _request(args, "GET", "/stats")
            _, state = _request(args, "GET", "/state")
            params = {"limit": args.limit}
            if args.channel:
                params["channel"] = args.channel
            _, signals = _request(args, "GET", "/signals", params=params)
            print("\033[2J\033[H", end="")
            print(_color(f"guardian+ simulator @ {args.url}  ({time.strftime('%H:%M:%S')})", CYAN))
            print()
            _render_stats(stats)
            print()
            print("Devices")
            _render_state(state["devices"])
            print()
            print(f"Last {args.limit} signals" + (f" on {args.channel}" if args.channel else ""))
            _render_signals(signals["signals"])
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()


def cmd_listen(args):
    """Se suscribe al broker para comprobar que la telemetria llega de verdad."""
    import paho.mqtt.client as mqtt

    topic = args.topic or (f"{args.topic_prefix}/{args.channel}/#" if args.channel
                           else f"{args.topic_prefix}/#")
    received = {"count": 0}

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", reason_code != 0):
            print(_color(f"Connection refused by broker: {reason_code}", RED), file=sys.stderr)
            client.disconnect()
            return
        client.subscribe(topic, qos=1)
        print(_color(f"Subscribed to {topic} on {args.mqtt_host}:{args.mqtt_port} - Ctrl+C to stop", DIM),
              flush=True)

    def on_message(client, userdata, message):
        received["count"] += 1
        try:
            body = json.loads(message.payload.decode())
        except (ValueError, UnicodeDecodeError):
            print(f"{time.strftime('%H:%M:%S')}  {message.topic}  {message.payload!r}", flush=True)
            return
        severity = body.get("severity", "INFO")
        label = _color(f"{body.get('signalType', '?'):<22}", _severity_color(severity))
        detail = json.dumps(body) if args.full else ""
        print(f"{time.strftime('%H:%M:%S')}  {_color('RECV', GREEN)}  {label} {message.topic}  {detail}",
              flush=True)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(args.mqtt_host, args.mqtt_port, keepalive=60)
    except OSError as e:
        print(_color(f"Cannot reach the broker at {args.mqtt_host}:{args.mqtt_port}: {e}", RED),
              file=sys.stderr)
        raise SystemExit(2)
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        client.disconnect()
    print(f"\n{received['count']} message(s) received.")


# ---------------- parser ----------------

def build_parser():
    # opciones aceptadas antes o despues del subcomando
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=argparse.SUPPRESS,
                        help=f"simulator API base url (default {DEFAULT_URL})")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print the raw JSON response")

    parser = argparse.ArgumentParser(
        prog="simulator",
        description="Control and monitoring CLI for the Guardian+ IoT wearable simulator.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the simulator (API + emission loop)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.environ.get("SIMULATOR_PORT", 5000)))
    p.add_argument("--mqtt-host", default=MQTT_HOST)
    p.add_argument("--mqtt-port", type=int, default=MQTT_PORT)
    p.add_argument("--topic-prefix", default=TOPIC_PREFIX)
    p.add_argument("--backend-url", default=BACKEND_DEVICES_URL)
    p.add_argument("--interval", type=int, default=int(os.environ.get("EMIT_INTERVAL_SECONDS", 3)),
                   help="seconds between simulation cycles")
    p.add_argument("--minutes-per-tick", type=float, default=5.0,
                   help="simulated minutes each cycle represents")
    p.add_argument("--consecutive-hits", type=int, default=4,
                   help="readings an anomaly episode lasts (backend tolerance rule)")
    p.add_argument("--anomaly-chance", type=float, default=0.05)
    p.add_argument("--fall-chance", type=float, default=0.004)
    p.add_argument("--sos-chance", type=float, default=0.003)
    p.add_argument("--inactivity-minutes", type=int, default=60)
    p.add_argument("--safe-zone-radius", type=float, default=200.0, help="meters")
    p.add_argument("--battery-drain", type=float, default=0.35, help="battery %% lost per cycle")
    p.add_argument("--alerts-qos", type=int, choices=[0, 1, 2], default=1)
    p.add_argument("--telemetry-qos", type=int, choices=[0, 1, 2], default=0)
    p.add_argument("--max-rate", type=float, default=float(os.environ.get("MQTT_MAX_RATE", 0)),
                   help="max messages per second to the broker, 0 = unlimited (critical alerts are never held back)")
    p.add_argument("--burst", type=int, default=int(os.environ.get("MQTT_BURST", 20)),
                   help="messages allowed in a burst before the rate limit kicks in")
    p.add_argument("--retain", action="store_true",
                   default=os.environ.get("MQTT_RETAIN", "").lower() in ("1", "true", "yes"),
                   help="publish with the retain flag so the broker keeps the last message per topic")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_serve)

    sub.add_parser("health", parents=[common], help="quick liveness check").set_defaults(func=cmd_health)
    sub.add_parser("status", parents=[common], help="devices currently loaded").set_defaults(func=cmd_status)
    sub.add_parser("state", parents=[common], help="live per-device state (battery, position, episodes)").set_defaults(func=cmd_state)
    sub.add_parser("stats", parents=[common], help="full simulator + MQTT publisher stats").set_defaults(func=cmd_stats)
    sub.add_parser("update", parents=[common], help="reload devices from the backend").set_defaults(func=cmd_update)
    sub.add_parser("types", parents=[common], help="vital sign catalogue and its clinical thresholds").set_defaults(func=cmd_types)

    p = sub.add_parser("signals", parents=[common], help="recent signals and whether they were sent")
    p.add_argument("-n", "--limit", type=int, default=15)
    p.add_argument("-c", "--channel", choices=CHANNELS)
    p.add_argument("-d", "--device-id")
    p.add_argument("--critical", action="store_true", help="only critical alerts")
    p.set_defaults(func=cmd_signals)

    p = sub.add_parser("devices", parents=[common], help="set device ids manually (test without the backend)")
    p.add_argument("device_ids", nargs="+")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("emit", parents=[common], help="publish one event right now")
    p.add_argument("event", nargs="?", default="cycle", choices=EVENTS,
                   help="event to force (default: cycle = the full telemetry burst)")
    p.add_argument("--device-id", help="device to emit for (default: random from the loaded ones)")
    p.add_argument("--vital-sign-type", choices=VITAL_SIGN_CODES, help="for 'anomaly'")
    p.add_argument("--direction", choices=["high", "low"], help="for 'anomaly'")
    p.set_defaults(func=cmd_emit)

    p = sub.add_parser("loop", parents=[common], help="start or stop the emission loop")
    p.add_argument("action", choices=["start", "stop"])
    p.set_defaults(func=cmd_loop)

    p = sub.add_parser("watch", parents=[common], help="live dashboard, refreshed periodically")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("-n", "--limit", type=int, default=12)
    p.add_argument("-c", "--channel", choices=CHANNELS)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("listen", help="subscribe to the broker to verify signals actually arrive")
    p.add_argument("--mqtt-host", default=MQTT_HOST)
    p.add_argument("--mqtt-port", type=int, default=MQTT_PORT)
    p.add_argument("--topic-prefix", default=TOPIC_PREFIX)
    p.add_argument("-c", "--channel", choices=CHANNELS, help="listen to one channel only")
    p.add_argument("--topic", help="full topic filter (overrides --channel)")
    p.add_argument("--full", action="store_true", help="print the whole JSON payload")
    p.set_defaults(func=cmd_listen)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    # --url/--json usan SUPPRESS para poder ir antes o despues del subcomando
    args.url = getattr(args, "url", DEFAULT_URL)
    args.json = getattr(args, "json", False)
    args.func(args)


if __name__ == "__main__":
    main()
