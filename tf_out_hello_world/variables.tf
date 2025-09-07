variable "project" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "GCP zone"
  type        = string
  default     = "us-central1-a"
}

variable "machine_type" {
  description = "GCE machine type (e.g., e2-small, e2-standard-2)"
  type        = string
  default     = "e2-small"
}

variable "disk_size_gb" {
  description = "Boot disk size in GB"
  type        = number
  default     = 30
}

variable "image" {
  description = "Boot image (project/family or project/image)"
  type        = string
  default     = "ubuntu-os-cloud/ubuntu-2204-lts"
}

variable "app_port" {
  description = "The port the app listens on inside the VM/container"
  type        = number
  default     = 5000
}
