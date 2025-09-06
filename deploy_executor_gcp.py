# deploy_executor_gcp.py
import os
import uuid
import json
import subprocess
import tempfile
from pathlib import Path

# ----------------------------
# Terraform templates
# ----------------------------
TF_MAIN = """\
terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
  required_version = ">= 1.5.0"
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

# Enable Compute API (no-op if already enabled)
resource "google_project_service" "compute" {
  project            = var.project_id
  service            = "compute.googleapis.com"
  disable_on_destroy = false
}

# Firewall rule for the app port
resource "google_compute_firewall" "fw" {
  name    = "${var.name_prefix}-fw"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = [tostring(var.app_port)]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["${var.name_prefix}"]
}

# Ubuntu 22.04 LTS
data "google_compute_image" "ubuntu" {
  family  = "ubuntu-2204-lts"
  project = "ubuntu-os-cloud"
}

resource "google_compute_instance" "app" {
  name         = "${var.name_prefix}-app"
  machine_type = var.machine_type
  tags         = ["${var.name_prefix}"]

  boot_disk {
    initialize_params {
      image = data.google_compute_image.ubuntu.self_link
      size  = 20
      type  = "pd-balanced"
    }
  }

  network_interface {
    network = "default"
    access_config {} # external IP
  }

  metadata_startup_script = templatefile("${path.module}/startup.tftpl", {
    repo_url      = var.repo_url
    start_command = var.start_command
    app_port      = var.app_port
    env_lines     = join("\\n", var.env_lines)
    language      = var.language
  })

  depends_on = [google_project_service.compute]
}

output "external_ip" {
  value = google_compute_instance.app.network_interface[0].access_config[0].nat_ip
}

output "name_prefix" {
  value = var.name_prefix
}
"""

TF_VARS = """\
variable "name_prefix"   { type = string }
variable "project_id"    { type = string }
variable "region"        { type = string }
variable "zone"          { type = string }
variable "machine_type"  { type = string }
variable "repo_url"      { type = string }
variable "start_command" { type = string }
variable "app_port"      { type = number }
variable "language"      { type = string }
variable "env_lines"     { type = list(string) }
"""

# Minimal, robust startup: installs deps, clones repo, exports env, opens port, runs app
STARTUP = r"""#!/usr/bin/env bash
set -euo pipefail

echo "[startup] begin" | tee /var/log/autodeploy.log

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git curl

mkdir -p /opt/app && cd /opt/app
if [ ! -d app ]; then
  git clone ${repo_url} app
fi
cd app

if [ "${language}" = "python" ]; then
  apt-get install -y python3-pip python3-venv
  python3 -m venv .venv
  . .venv/bin/activate
  if [ -f requirements.txt ]; then
    pip install --upgrade pip
    pip install -r requirements.txt
  fi
elif [ "${language}" = "node" ]; then
  curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
  apt-get install -y nodejs
  if [ -f package.json ]; then
    npm install
  fi
fi

# Export env vars for login shells and current shell
cat <<'EOF' >/etc/profile.d/app_env.sh
${env_lines}
EOF
chmod +x /etc/profile.d/app_env.sh
. /etc/profile.d/app_env.sh || true

# Replace localhost bindings (best-effort)
grep -rl "127.0.0.1" . | xargs -I{} sed -i "s/127.0.0.1/0.0.0.0/g" {} || true
grep -rl "localhost" .  | xargs -I{} sed -i "s/localhost/0.0.0.0/g" {} || true

# Open the app port at the OS level (VPC firewall already created)
which ufw >/dev/null 2>&1 || apt-get install -y ufw
ufw allow ${app_port} || true
ufw --force enable || true

# Run app as background process with logs
nohup bash -lc "${start_command}" >> /var/log/app_start.log 2>&1 &

echo "[startup] done" | tee -a /var/log/autodeploy.log
"""

# ----------------------------
# Helpers
# ----------------------------
def run(cmd, cwd=None):
    print("$", " ".join(cmd))
    p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if p.returncode != 0:
        print(p.stdout)
        print(p.stderr)
        raise RuntimeError(f"Command failed: {' '.join(cmd)}")
    return p.stdout.strip()

def write_tf_project(workdir: Path, spec: dict):
    # Write TF files
    (workdir / "main.tf").write_text(TF_MAIN)
    (workdir / "variables.tf").write_text(TF_VARS)
    (workdir / "startup.tftpl").write_text(STARTUP)

    # Build env_lines
    env_lines = [f'export {k}="{str(v)}"' for k, v in (spec.get("env") or {}).items()]

    # Unique name prefix each run (or allow caller-provided)
    prefix = spec.get("name_prefix") or f"autodeploy-{uuid.uuid4().hex[:6]}"

    tfvars = {
        "name_prefix":   prefix,
        "project_id":    spec["project_id"],
        "region":        spec.get("region", "us-central1"),
        "zone":          spec.get("zone", "us-central1-a"),
        "machine_type":  spec.get("machine_type", "e2-small"),
        "repo_url":      spec["repo_url"],
        "start_command": spec["start_command"],
        "app_port":      int(spec["port"]),
        "language":      spec.get("language", "python"),
        "env_lines":     env_lines,
    }
    (workdir / "terraform.tfvars.json").write_text(json.dumps(tfvars, indent=2))

def deploy_on_gcp(spec: dict):
    """
    Creates a unique-named VM + firewall on every run to avoid collisions.
    Required keys in spec: project_id, repo_url, start_command, port
    Optional: region, zone, machine_type, env (dict), language, name_prefix
    """
    with tempfile.TemporaryDirectory(prefix="autodeploy-gcp-") as td:
        wd = Path(td)
        write_tf_project(wd, spec)

        # Terraform apply
        run(["terraform", "init", "-input=false"], cwd=wd)
        run(["terraform", "apply", "-auto-approve"], cwd=wd)

        out = run(["terraform", "output", "-json"], cwd=wd)
        outputs = json.loads(out)
        ip = outputs["external_ip"]["value"]
        name_prefix = outputs.get("name_prefix", {}).get("value", None)
        url = f"http://{ip}:{spec['port']}"

        print("\n=== Deployment Complete (GCP) ===")
        print("External IP:", ip)
        print("Name prefix:", name_prefix or "(unknown)")
        print("App URL:    ", url)
        print("Logs: /var/log/autodeploy.log and /var/log/app_start.log (ssh if needed)")
        return {"external_ip": ip, "url": url, "name_prefix": name_prefix}
