locals {
  tag       = var.name
  mqtt_host = var.deploy_broker ? "localhost" : var.external_mqtt_host

  # puertos expuestos: API del simulador y, si va en la VM, el broker
  app_ports = concat(
    [tostring(var.simulator_port)],
    var.deploy_broker ? [tostring(var.mqtt_port), tostring(var.mqtt_ws_port)] : [],
  )
}

resource "google_project_service" "compute" {
  service            = "compute.googleapis.com"
  disable_on_destroy = false
}

resource "google_service_account" "vm" {
  account_id   = "${var.name}-vm"
  display_name = "Guardian+ IoT simulator VM"
}

# solo lo necesario para mandar logs y metricas del agente de Ops
resource "google_project_iam_member" "vm" {
  for_each = toset(["roles/logging.logWriter", "roles/monitoring.metricWriter"])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.vm.email}"
}

resource "google_compute_address" "vm" {
  name       = "${var.name}-ip"
  region     = var.region
  depends_on = [google_project_service.compute]
}

resource "google_compute_firewall" "app" {
  name          = "${var.name}-app"
  network       = var.network
  target_tags   = [local.tag]
  source_ranges = var.allowed_source_ranges

  allow {
    protocol = "tcp"
    ports    = local.app_ports
  }

  depends_on = [google_project_service.compute]
}

resource "google_compute_firewall" "ssh" {
  name          = "${var.name}-ssh"
  network       = var.network
  target_tags   = [local.tag]
  source_ranges = var.ssh_source_ranges

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  depends_on = [google_project_service.compute]
}

resource "google_compute_instance" "vm" {
  name         = var.name
  machine_type = var.machine_type
  zone         = var.zone
  tags         = [local.tag]

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 20
    }
  }

  network_interface {
    network = var.network
    access_config {
      nat_ip = google_compute_address.vm.address
    }
  }

  service_account {
    email  = google_service_account.vm.email
    scopes = ["cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot = true
  }

  metadata = {
    enable-oslogin = "TRUE"
  }

  # corre en cada arranque; es idempotente
  metadata_startup_script = templatefile("${path.module}/startup.sh.tftpl", {
    repo_url                   = var.repo_url
    repo_ref                   = var.repo_ref
    deploy_broker              = var.deploy_broker
    mqtt_host                  = local.mqtt_host
    mqtt_port                  = var.mqtt_port
    mqtt_ws_port               = var.mqtt_ws_port
    mqtt_retain                = var.mqtt_retain
    mqtt_max_rate              = var.mqtt_max_rate
    mqtt_burst                 = var.mqtt_burst
    topic_prefix               = var.topic_prefix
    simulator_port             = var.simulator_port
    backend_devices_url        = var.backend_devices_url
    emit_interval_seconds      = var.emit_interval_seconds
    broker_max_queued_messages = var.broker_max_queued_messages
    broker_session_expiry      = var.broker_session_expiry
  })

  lifecycle {
    precondition {
      condition     = var.deploy_broker || var.external_mqtt_host != ""
      error_message = "Set external_mqtt_host when deploy_broker = false."
    }
  }

  depends_on = [google_project_iam_member.vm]
}
