#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
write_tf_gcp_from_report.py

Generates a Terraform GCP (Compute Engine VM) deployment bundle from an analyzer report.

Usage:
  python write_tf_gcp_from_report.py <github_repo_url> \
    --project cd47-proj \
    --region us-central1 \
    --zone us-central1-a \
    [--env-json env_report.json] \
    [--machine-type e2-small] \
    [--disk-size 30] \
    [--image docker.io/you/yourapp:latest]

Behavior:
- Reads env report from --env-json (default: env_report.json, else <repo>_env.json).
- Writes ./tf_out_<repo> with: main.tf, variables.tf, outputs.tf, terraform.tfvars.json, startup.sh
- startup.sh flow:
    1) Try to pull provided image; if it’s only arm64, enables binfmt and pulls arm64.
    2) If pull fails (or no image provided), git clone the repo and docker build locally.
    3) If no Dockerfile, run natively via systemd (Python or Node) using start command/env from the report.
"""

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlparse

# ---------- helpers ----------

def repo_name_from_url(repo_url: str) -> str:
    name = urlparse(repo_url).path.rstrip("/").split("/")[-1]
    return name[:-4] if name.endswith(".git") else name

def load_report(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"[error] env report not found: {path}")
    try:
        return json.loads(path.read_text())
    except Exception as e:
        raise SystemExit(f"[error] failed to parse report JSON: {e}")

def prefer_port(ports):
    if not ports:
        return 8000
    ints = []
    for p in ports:
        if isinstance(p, (int, float)):
            ints.append(int(p))
        elif isinstance(p, str) and p.strip().isdigit():
            ints.append(int(p.strip()))
    for cand in (80, 8080, 5000, 8000):
        if cand in ints:
            return cand
    return ints[0] if ints else 8000

def infer_config(repo_url: str, report: dict, args):
    # base cfg (CLI overrides > report > defaults)
    cfg = {
        "project": args.project,
        "region": args.region or "us-central1",
        "zone": args.zone or "us-central1-a",
        "repo_url": repo_url,
        "public_image": args.image or report.get("public_image") or "",  # optional now
        "machine_type": args.machine_type or "e2-small",
        "disk_size_gb": int(args.disk_size) if args.disk_size else 30,
        "image": "ubuntu-os-cloud/ubuntu-2204-lts",
        "language": (report.get("language") or "").lower(),
        "app_port": prefer_port(report.get("ports") or []),
        "env_lines": [],
        "start_command": "",
    }

    # env lines from report
    seen = set()
    for e in (report.get("env_vars") or []):
        n = e.get("name")
        d = e.get("default")
        if n and n not in seen:
            seen.add(n)
            if d not in (None, ""):
                cfg["env_lines"].append(f"{n}={d}")
    if "PORT" not in seen:
        cfg["env_lines"].append(f"PORT={cfg['app_port']}")
    if "HOST" not in seen:
        cfg["env_lines"].append("HOST=0.0.0.0")

    # start command
    starts = report.get("start_commands") or []
    if starts:
        cfg["start_command"] = str(starts[0]).strip()
    else:
        cfg["start_command"] = "npm start" if cfg["language"].startswith("node") else "python3 app.py"

    return cfg

def write_file(path: Path, content: str):
    path.write_text(content.strip() + "\n")

# ---------- writers ----------

def write_startup(outdir: Path, cfg: dict):
    image = cfg.get("public_image") or ""
    app_port = cfg["app_port"]
    repo_url = cfg["repo_url"]
    language = (cfg.get("language") or "").lower()
    # escape for single-quoted shell
    start_cmd = (cfg.get("start_command") or f"gunicorn --bind 0.0.0.0:${{PORT:-{app_port}}} app:app || python3 app.py").replace("'", "'\"'\"'")

    startup = f"""#!/bin/bash
set -euxo pipefail

mkdir -p /var/log/autodeploy
exec > >(tee -a /var/log/autodeploy/startup.log) 2>&1

# Base tools
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io git curl ca-certificates python3 python3-venv python3-pip
systemctl enable --now docker || true

IMAGE="{image}"
PLATFORM=""
ARCH_OK=0
PULL_OK=0

# Env file
install -d -m 0755 /opt
cat > /opt/app.env <<'EOF'
{os.linesep.join(cfg.get('env_lines', []))}
EOF

# Pull (multi-arch aware)
if [ -n "$IMAGE" ]; then
  if docker manifest inspect "$IMAGE" >/dev/null 2>&1 && \
     docker manifest inspect "$IMAGE" | grep -q '"architecture": "amd64"'; then
    ARCH_OK=1
  fi
  if [ "$ARCH_OK" -eq 1 ]; then
    docker pull "$IMAGE" && PULL_OK=1 || true
  else
    docker run --privileged --rm tonistiigi/binfmt --install arm64 || true
    PLATFORM="--platform linux/arm64"
    docker pull $PLATFORM "$IMAGE" && PULL_OK=1 || true
  fi
fi

# Try to run pulled image with explicit shell command first
if [ "$PULL_OK" -eq 1 ]; then
  echo "[info] Running pulled image with explicit start command"
  docker rm -f autodeploy-app || true
  if docker run -d --name autodeploy-app $PLATFORM --env-file /opt/app.env -p 80:{app_port} -w /app \
       --entrypoint /bin/sh "$IMAGE" -lc '{start_cmd}'; then
    exit 0
  fi
  echo "[warn] Explicit start failed; trying image default CMD/ENTRYPOINT"
  docker rm -f autodeploy-app || true
  if docker run -d --name autodeploy-app $PLATFORM --env-file /opt/app.env -p 80:{app_port} "$IMAGE"; then
    exit 0
  fi
  echo "[warn] Pulled image failed to run; falling back to source."
fi

# Fallback: build/run from source
echo "[info] Cloning and running from source"
rm -rf /opt/app-src
git clone --depth=1 "{repo_url}" /opt/app-src

if [ -f /opt/app-src/Dockerfile ]; then
  echo "[info] Dockerfile present; building local image"
  docker build -t autodeploy-local:latest /opt/app-src
  docker rm -f autodeploy-app || true
  if docker run -d --name autodeploy-app --env-file /opt/app.env -p 80:{app_port} -w /app \
       --entrypoint /bin/sh autodeploy-local:latest -lc '{start_cmd}'; then
    exit 0
  fi
  echo "[warn] Local image still failed; trying default CMD"
  docker rm -f autodeploy-app || true
  docker run -d --name autodeploy-app --env-file /opt/app.env -p 80:{app_port} autodeploy-local:latest
  exit 0
fi

# Native run (no Dockerfile)
echo "[warn] No Dockerfile; running natively via systemd"
if echo "{language}" | grep -Eiq '^python'; then
  cd /opt/app-src
  python3 -m venv /opt/app-venv
  . /opt/app-venv/bin/activate
  [ -f requirements.txt ] && pip install -r requirements.txt || true
  pip install gunicorn || true
  cat >/etc/systemd/system/autodeploy.service <<SERVICE
[Unit]
Description=Autodeploy App (Python)
After=network.target
[Service]
EnvironmentFile=/opt/app.env
WorkingDirectory=/opt/app-src
ExecStart=/bin/bash -lc '{start_cmd}'
Restart=always
User=root
[Install]
WantedBy=multi-user.target
SERVICE
  systemctl daemon-reload
  systemctl enable --now autodeploy.service
  exit 0
fi

if echo "{language}" | grep -Eiq '^node'; then
  curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
  DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs
  cd /opt/app-src
  [ -f package.json ] && (npm ci || npm install)
  cat >/etc/systemd/system/autodeploy.service <<SERVICE
[Unit]
Description=Autodeploy App (Node)
After=network.target
[Service]
EnvironmentFile=/opt/app.env
WorkingDirectory=/opt/app-src
ExecStart=/bin/bash -lc '{start_cmd}'
Restart=always
User=root
[Install]
WantedBy=multi-user.target
SERVICE
  systemctl daemon-reload
  systemctl enable --now autodeploy.service
  exit 0
fi

echo "[error] Unknown stack; manual start required."
exit 1
"""
    write_file(outdir / "startup.sh", startup)


def write_main_tf(outdir: Path):
    # NOTE: not an f-string → Terraform braces are safe here
    main_tf = """\
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
"""
    write_file(outdir / "main.tf", main_tf)

def write_variables_tf(outdir: Path):
    # ✅ FIXED: valid multi-line HCL blocks (no semicolons)
    variables_tf = """\
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
  description = "GCE machine type"
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
  description = "Internal app port (container/native)"
  type        = number
  default     = 8000
}
"""
    write_file(outdir / "variables.tf", variables_tf)

def write_outputs_tf(outdir: Path):
    outputs_tf = """\
output "public_ip" {
  description = "Public IP of the VM"
  value       = google_compute_instance.app.network_interface[0].access_config[0].nat_ip
}
"""
    write_file(outdir / "outputs.tf", outputs_tf)

def write_tfvars(outdir: Path, cfg: dict):
    (outdir / "terraform.tfvars.json").write_text(json.dumps({
        "project": cfg["project"],
        "region": cfg["region"],
        "zone": cfg["zone"],
        "machine_type": cfg["machine_type"],
        "disk_size_gb": cfg["disk_size_gb"],
        "image": cfg["image"],
        "app_port": cfg["app_port"],
    }, indent=2) + "\n")

# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description="Write Terraform bundle (GCP VM) from env report with robust startup fallbacks.")
    ap.add_argument("repo_url", help="GitHub repo URL (used for naming and clone fallback)")
    ap.add_argument("--project", required=True, help="GCP project ID")
    ap.add_argument("--region", default="us-central1", help="GCP region")
    ap.add_argument("--zone", default="us-central1-a", help="GCP zone")
    ap.add_argument("--env-json", default=None, help="Path to env_report.json")
    ap.add_argument("--machine-type", dest="machine_type", default="e2-small", help="GCE machine type")
    ap.add_argument("--disk-size", dest="disk_size", default="30", help="Boot disk size GB")
    ap.add_argument("--image", default=None, help="Optional container image to pull (e.g., docker.io/you/app:latest)")
    args = ap.parse_args()

    repo = repo_name_from_url(args.repo_url)
    default_env = Path("env_report.json")
    alt_env = Path(f"{repo}_env.json")
    env_path = Path(args.env_json) if args.env_json else (default_env if default_env.exists() else alt_env)
    report = load_report(env_path)

    cfg = infer_config(args.repo_url, report, args)

    outdir = Path(f"./tf_out_{repo}")
    outdir.mkdir(parents=True, exist_ok=True)
    write_startup(outdir, cfg)
    write_main_tf(outdir)
    write_variables_tf(outdir)
    write_outputs_tf(outdir)
    write_tfvars(outdir, cfg)

    print(f"[✓] Terraform bundle written to {outdir}")
    print("    Files: main.tf, variables.tf, outputs.tf, terraform.tfvars.json, startup.sh")
    print("Next:")
    print(f"  cd {outdir} && terraform init && terraform apply -auto-approve")
    print("Logs on VM: /var/log/autodeploy/startup.log")
    print("Open http://<public_ip>/ after apply.")

if __name__ == "__main__":
    main()
