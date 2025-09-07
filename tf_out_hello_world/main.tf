terraform {
  required_version = ">= 1.3.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5"
    }
  }
}

provider "google" {
  project = var.project
  region  = var.region
  zone    = var.zone
}

resource "random_id" "suffix" {
  byte_length = 2
}

locals {
  app_name      = "autodeploy-app-${random_id.suffix.hex}"
  firewall_http = "autodeploy-allow-http-${random_id.suffix.hex}"
  firewall_app  = "autodeploy-allow-app-${random_id.suffix.hex}"
}

# Allow HTTP/80 for the container/native app
resource "google_compute_firewall" "http" {
  name    = local.firewall_http
  network = "default"
  allow {
    protocol = "tcp"
    ports    = ["80"]
  }
  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["http-server"]
}

# Also allow the internal app port for debugging (optional)
resource "google_compute_firewall" "app" {
  name    = local.firewall_app
  network = "default"
  allow {
    protocol = "tcp"
    ports    = [tostring(var.app_port)]
  }
  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["http-server"]
}

resource "google_compute_instance" "app" {
  name         = local.app_name
  machine_type = var.machine_type
  zone         = var.zone

  boot_disk {
    initialize_params {
      image = var.image
      size  = var.disk_size_gb
    }
  }

  network_interface {
    network = "default"
    access_config {}
  }

  tags = ["http-server"]

  metadata_startup_script = file("startup.sh")

  service_account {
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }

  depends_on = [
    google_compute_firewall.http,
    google_compute_firewall.app
  ]
}
