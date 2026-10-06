# guardian-plus-iot-simulator

IoT simulator for the Guardian+ wearable. It pulls the wearable devices from the
backend, simulates the telemetry of a bracelet worn by a Fragile Citizen and
publishes it to an MQTT broker, exposing an HTTP API plus a CLI to check how it
is running and whether the signals were actually sent.

Unlike a single-signal sensor, the Guardian+ bracelet emits several families of
telemetry, each one on its own channel so every Bounded Context subscribes only
to what it consumes:

| Channel | Bounded Context | Signals |
|---|---|---|
| `vitals` | Health Monitoring | `VITAL_SIGN_READING` (HR, BP_SYS, BP_DIA, SPO2, TEMP, RESP_RATE) |
| `alerts` | Emergency & Alerting | `FALL_DETECTED`, `FALL_CONFIRMED`, `FALL_CANCELLED`, `SOS_TRIGGERED`, `BATTERY_LOW`, `BATTERY_CRITICAL` |
| `location` | Mobility & Geofencing | `LOCATION_UPDATE` |
| `activity` | Care Routines & Wellness | `ACTIVITY_SAMPLE`, `ACTIVITY_RESUMED`, `PROLONGED_INACTIVITY` |
| `sleep` | Care Routines & Wellness | `SLEEP_CYCLE_RECORDED` |

The simulator keeps **state per device** — battery, position, inactivity counter,
clinical episodes in progress — so the telemetry is coherent over time: the
battery drains, the patient walks away and comes back, and an anomaly lasts
several consecutive readings, which is what the backend's tolerance rule needs
before it confirms a `VitalSignAnomalyDetectedIntegrationEvent`.

## Install

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python simulator/cli.py serve                     # or: python simulator/main.py
```

Configuration comes from flags or environment variables:

| Variable | Flag | Default |
|---|---|---|
| `MQTT_HOST` | `--mqtt-host` | `localhost` |
| `MQTT_PORT` | `--mqtt-port` | `1883` |
| `MQTT_TOPIC_PREFIX` | `--topic-prefix` | `guardian` |
| `BACKEND_DEVICES_URL` | `--backend-url` | `http://localhost:8080/api/v1/wearable-devices` |
| `SIMULATOR_PORT` | `--port` | `5000` |
| `EMIT_INTERVAL_SECONDS` | `--interval` | `10` |
| — | `--minutes-per-tick` | `5` (simulated minutes per cycle) |
| — | `--consecutive-hits` | `4` (readings an anomaly episode lasts) |
| — | `--anomaly-chance` / `--fall-chance` / `--sos-chance` | `0.05` / `0.004` / `0.003` |
| — | `--inactivity-minutes` | `60` |
| — | `--safe-zone-radius` | `200` (meters) |
| — | `--battery-drain` | `0.35` (% per cycle) |
| — | `--alerts-qos` / `--telemetry-qos` | `1` / `0` |
| `MQTT_MAX_RATE` | `--max-rate` | `0` (messages/second to the broker, 0 = no limit) |
| `MQTT_BURST` | `--burst` | `20` (messages allowed in a burst) |
| `MQTT_RETAIN` | `--retain` | off (broker keeps the last message per topic) |

> On macOS, port 5000 is taken by the AirPlay Receiver. Use `--port 5055` or
> disable it in System Settings.

The MQTT connection is made in the background: if the broker is down the simulator
still starts, keeps retrying and reconnects on its own. Signals published while
disconnected are counted as failed and shown as such by the CLI. Devices are
loaded from the backend at startup; if the backend is not up yet, the simulator
starts anyway and you can load devices by hand or call `update` later.

Signals are published to `guardian/<channel>/<deviceId>`, e.g.
`guardian/vitals/GP-WB-001`:

```json
{
  "deviceId": "GP-WB-001",
  "careRecipientProfileId": "aaaaaaaa-0000-4000-8000-000000000001",
  "signalType": "VITAL_SIGN_READING",
  "severity": "INFO",
  "measuredAt": "2026-09-25T16:45:09.423159+00:00",
  "vitalSignTypeCode": "SPO2",
  "vitalSignTypeName": "Saturacion de oxigeno",
  "value": 88.0,
  "unit": "%",
  "status": "CRITICAL_LOW",
  "normalRange": {"min": 95, "max": 100},
  "sustained": true
}
```

Critical alerts (`severity: "CRITICAL"`) are published with **QoS 1** — losing a
fall or an SOS is not an option — while routine telemetry uses QoS 0.

`--max-rate` turns on a token-bucket rate limiter in front of the broker. Telemetry
over the limit waits for its turn instead of being dropped, so nothing is lost; it
just gets spaced out. Critical alerts never wait. `stats` shows how many messages
were held back and for how long.

## Clinical thresholds

Taken from the acceptance criteria in chapter II of the report (US01–US05). Each
reading is classified as `NORMAL`, `HIGH`/`LOW` or `CRITICAL_HIGH`/`CRITICAL_LOW`;
run `python simulator/cli.py types` to see the live catalogue.

| Code | Unit | Normal | Critical outside |
|---|---|---|---|
| `HR` | bpm | 60–100 | <50 or >130 |
| `BP_SYS` | mmHg | 90–120 | <90 or >140 |
| `BP_DIA` | mmHg | 60–80 | <60 or >90 |
| `SPO2` | % | 95–100 | <90 |
| `TEMP` | °C | 36.0–37.2 | <35.0 or >37.8 |
| `RESP_RATE` | rpm | 12–20 | <10 or >24 |

Other rules the simulator honours:

* **Fall** — `FALL_DETECTED` opens a 20 s local cancellation window, then the
  simulator emits `FALL_CANCELLED` (the citizen confirmed their wellbeing) or
  `FALL_CONFIRMED` with GPS coordinates (the timer expired with no response).
* **Prolonged inactivity** — after 60 min without movement; the counter resets on
  motion and emits `ACTIVITY_RESUMED` without raising an alarm.
* **Battery** — `BATTERY_LOW` at 20%, `BATTERY_CRITICAL` at 5%.
* **Sleep** — more than 4 interruptions classifies the night as `FRAGMENTED`.
* **Safe zone** — locations further than the configured radius are flagged
  `outsideSafeZone`.

## CLI

All commands except `serve` and `listen` talk to a running simulator over HTTP.
Point them somewhere else with `--url` or `SIMULATOR_URL` (e.g. the VM's address);
add `--json` to any of them for the raw response.

```bash
python simulator/cli.py health            # is it alive, is MQTT connected, is the loop running
python simulator/cli.py stats             # full picture: devices, loop, broker, counters per channel
python simulator/cli.py state             # per-device battery, position, inactivity, open episodes
python simulator/cli.py types             # vital sign catalogue and its thresholds
python simulator/cli.py signals -n 20     # last signals: sent ok / FAIL, and whether the broker acked
python simulator/cli.py signals -c alerts # only one channel
python simulator/cli.py signals --critical
python simulator/cli.py watch             # live dashboard, refreshed every 2s
python simulator/cli.py status            # devices currently loaded
python simulator/cli.py update            # reload the device list from the backend
python simulator/cli.py devices GP-WB-001 GP-WB-002   # load devices by hand, to test without the backend
python simulator/cli.py emit              # emit one full telemetry cycle right now
python simulator/cli.py emit fall         # force a fall (its outcome follows 20s later)
python simulator/cli.py emit sos
python simulator/cli.py emit anomaly --vital-sign-type SPO2 --direction low
python simulator/cli.py emit battery      # drop the battery to the warning threshold
python simulator/cli.py loop stop|start   # pause / resume the automatic emission
python simulator/cli.py listen            # subscribe to the broker and print the telemetry as it arrives
python simulator/cli.py listen -c alerts --full
```

`signals` is the direct answer to "did the publisher really send anything":

```
WHEN      DEVICE           CHANNEL   SIGNAL                 DETAIL                        SENT  ACK
16:47:57  GP-WB-101        alerts    FALL_CANCELLED         false positive after 7.6s     ok    ok
16:47:49  GP-WB-101        alerts    FALL_DETECTED          impact 3.18g, 20s to cancel   ok    ok
16:46:29  GP-WB-002        vitals    VITAL_SIGN_READING     SPO2 88.0 % (CRITICAL_LOW)    ok    ok
```

* `SENT` — the client accepted the publish (broker reachable).
* `ACK` — paho confirmed the message left the client towards the broker.
* `listen` is the independent check: it connects to the broker as a subscriber, so
  whatever it prints really made it across the network.

## HTTP API

| Method | Path | Description |
|---|---|---|
| GET | `/health` | liveness, MQTT connection, loop state |
| GET | `/status` | devices currently loaded |
| GET | `/state` | live simulated state of every device |
| GET | `/stats` | simulator + MQTT publisher counters |
| GET | `/signals?limit=N&channel=&deviceId=&severity=` | last published signals (max 200) |
| GET | `/vital-sign-types` | vital sign catalogue and thresholds |
| POST | `/update` | reload devices from the backend |
| PUT | `/devices` | `{"deviceIds": [...]}` or `{"devices": [{...}]}` — set devices manually |
| POST | `/emit` | `{"event": "CYCLE\|VITALS\|ANOMALY\|FALL\|SOS\|LOCATION\|ACTIVITY\|SLEEP\|BATTERY", "deviceId": "..."}` |
| POST | `/loop` | `{"action": "start"\|"stop"}` |

The device list is read from the backend's `WearableDevicesController`. The parser
is tolerant: it accepts a bare list or a `{"content": [...]}` page, and reads the
id from `deviceId`, `id` or `serialNumber`, and the patient from
`careRecipientProfileId`, `fragileCitizenId` or `personUnderCareId`. If the
backend does not send home coordinates, devices are scattered around Lima.

## Project layout

```
simulator/
  main.py                     entrypoint (= cli.py serve)
  cli.py                      control and monitoring CLI
  model/
    signal.py                 Signal, Device, VitalSignSpec catalogue, SimulationConfig
    signal_generator.py       per-device state and signal generation
  interfaces/
    mqtt_publisher.py         publishes to the broker and tracks what was really sent
    api.py                    HTTP API and emission loop
```

## Running on the VM

```bash
git clone <repo> && cd guardian-plus-iot-simulator
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
MQTT_HOST=<broker-host> BACKEND_DEVICES_URL=<backend>/api/v1/wearable-devices \
  .venv/bin/python simulator/cli.py serve
```

Then, from your machine, check it with:

```bash
python simulator/cli.py --url http://<vm-ip>:5000 watch
```

Open port 5000 only to whoever needs to monitor it — the API has no authentication,
and Flask's development server is not meant for production traffic.

## Deploying to Google Cloud with Terraform

`infra/terraform` creates a Debian 12 VM with a static IP, firewall rules and a
minimal service account. On boot it installs Mosquitto and runs the simulator as
systemd services:

* **Broker** — Mosquitto with on-disk persistence. Persistent subscribers
  (`clean_session=false`) get their QoS 1 messages queued while offline (up to
  `broker_max_queued_messages`), and retained messages survive restarts.
* **Simulator** — publishes to the local broker with the rate limiter on
  (`mqtt_max_rate`, default 20 msg/s) and `retain` on.

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # set project_id, allowed_source_ranges, backend_devices_url
terraform init
terraform apply

SIMULATOR_URL=$(terraform output -raw simulator_url) python ../../simulator/cli.py stats
python ../../simulator/cli.py listen --mqtt-host $(terraform output -raw external_ip)
```

The broker allows anonymous access, so set `allowed_source_ranges` to the IPs that
really need it. On the VM, `guardian-sim <command>` runs the CLI against the local
simulator and `journalctl -u guardian-simulator -f` follows its logs. To use a
broker you already have, set `deploy_broker = false` and `external_mqtt_host`.
