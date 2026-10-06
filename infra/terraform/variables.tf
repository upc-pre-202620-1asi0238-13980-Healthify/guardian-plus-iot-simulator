# ---------- GCP ----------

variable "project_id" {
  description = "GCP project to deploy into."
  type        = string
}

variable "region" {
  description = "Region for the static IP."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "Zone for the VM."
  type        = string
  default     = "us-central1-a"
}

variable "name" {
  description = "Prefix for every resource name."
  type        = string
  default     = "guardian-iot-sim"
}

variable "machine_type" {
  description = "VM machine type. e2-small is plenty for the simulator + Mosquitto."
  type        = string
  default     = "e2-small"
}

variable "network" {
  description = "VPC network the VM joins."
  type        = string
  default     = "default"
}

variable "allowed_source_ranges" {
  description = "CIDRs allowed to reach the simulator API and the MQTT broker (e.g. your backend's egress IP, your own IP). The broker is anonymous, so keep this narrow."
  type        = list(string)
}

variable "ssh_source_ranges" {
  description = "CIDRs allowed to SSH in. The default is Google's IAP range, for `gcloud compute ssh --tunnel-through-iap`."
  type        = list(string)
  default     = ["35.235.240.0/20"]
}

# ---------- app ----------

variable "repo_url" {
  description = "Git repository the VM clones the simulator from."
  type        = string
  default     = "https://github.com/upc-pre-202620-1asi0238-13980-Healthify/guardian-plus-iot-simulator.git"
}

variable "repo_ref" {
  description = "Branch, tag or commit to deploy."
  type        = string
  default     = "main"
}

variable "simulator_port" {
  description = "Port of the simulator HTTP API."
  type        = number
  default     = 5000
}

variable "backend_devices_url" {
  description = "Backend endpoint the simulator loads wearable devices from."
  type        = string
}

variable "emit_interval_seconds" {
  description = "Seconds between simulation cycles."
  type        = number
  default     = 10
}

variable "topic_prefix" {
  description = "MQTT topic prefix (`<prefix>/<channel>/<deviceId>`)."
  type        = string
  default     = "guardian"
}

# ---------- broker ----------

variable "deploy_broker" {
  description = "Install Mosquitto on the same VM. Set to false and fill external_mqtt_host to publish to a broker you already run."
  type        = bool
  default     = true
}

variable "external_mqtt_host" {
  description = "Broker host when deploy_broker = false."
  type        = string
  default     = ""
}

variable "mqtt_port" {
  description = "MQTT broker port."
  type        = number
  default     = 1883
}

variable "mqtt_ws_port" {
  description = "MQTT over WebSocket port of the broker on the VM, used by the backend's telemetry subscriber (HEALTH_MONITORING_MQTT_BROKER_URL)."
  type        = number
  default     = 9001
}

variable "mqtt_retain" {
  description = "Publish with the retain flag so the broker keeps the last message of every topic for late subscribers."
  type        = bool
  default     = true
}

variable "broker_max_queued_messages" {
  description = "Messages Mosquitto keeps queued per offline persistent subscriber (clean_session=false) before dropping."
  type        = number
  default     = 10000
}

variable "broker_session_expiry" {
  description = "How long Mosquitto keeps the session and queue of a disconnected persistent subscriber."
  type        = string
  default     = "7d"
}

# ---------- rate limiter ----------

variable "mqtt_max_rate" {
  description = "Max messages per second the simulator sends to the broker. 0 disables the limiter. CRITICAL alerts are never held back."
  type        = number
  default     = 20

  validation {
    condition     = var.mqtt_max_rate >= 0
    error_message = "mqtt_max_rate must be >= 0."
  }
}

variable "mqtt_burst" {
  description = "Messages allowed in a burst before the rate limit applies."
  type        = number
  default     = 20
}
