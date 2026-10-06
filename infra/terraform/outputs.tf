output "external_ip" {
  description = "Static public IP of the VM."
  value       = google_compute_address.vm.address
}

output "simulator_url" {
  description = "Simulator HTTP API. Use it with the CLI: SIMULATOR_URL=<this> python simulator/cli.py stats"
  value       = "http://${google_compute_address.vm.address}:${var.simulator_port}"
}

output "mqtt_broker" {
  description = "Broker the simulator publishes to, as seen from outside the VM."
  value       = var.deploy_broker ? "${google_compute_address.vm.address}:${var.mqtt_port}" : "${var.external_mqtt_host}:${var.mqtt_port}"
}

output "mqtt_websocket_broker" {
  description = "Broker URL for the backend (HEALTH_MONITORING_MQTT_BROKER_URL). Only when the broker runs on the VM."
  value       = var.deploy_broker ? "ws://${google_compute_address.vm.address}:${var.mqtt_ws_port}" : null
}

output "ssh_command" {
  description = "SSH into the VM through IAP."
  value       = "gcloud compute ssh ${google_compute_instance.vm.name} --zone ${var.zone} --project ${var.project_id} --tunnel-through-iap"
}
