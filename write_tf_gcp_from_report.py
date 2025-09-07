#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate a Terraform GCP (Compute Engine VM) deployment bundle from your analyzer report.
"""

import argparse
import json
import os
import re
import textwrap
from pathlib import Path
from urllib.parse import urlparse

import requests


# ---------------- Provider-agnostic chat helper ----------------
def chat_complete(messages, model=None, provider=None, timeout=60):
    """
    provider: "openai" or "openrouter" (auto-detect by env if None)
    Env:
      - OPENAI_API_KEY      (for provider=openai)
      - OPENROUTER_API_KEY  (for provider=openrouter)
      - AI_MODEL            (optional override)
      - AI_PROVIDER         (optional: "openai"|"openrouter")
    """
    prov = (provider or os.getenv("AI_PROVIDER") or "").strip().lower()
    if prov not in ("openai", "openrouter"):
        # auto-detect by which key is present
        if os.getenv("OPENROUTER_API_KEY"):
            prov = "openrouter"
        else:
            prov = "openai"

    if prov == "openrouter":
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY not set")
        url = "https://openrouter.ai/api/v1/chat/completions"
        model = model or os.getenv("AI_MODEL") or "openai/gpt-4o-mini"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost",
            "X-Title": "AutoDeploy sizing",
        }
        payload = {"model": model, "messages": messages, "temperature": 0}
    else:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        url = "https://api.openai.com/v1/chat/completions"
        model = model or os.getenv("AI_MODEL") or "gpt-4o-mini"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": messages, "temperature": 0}

    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


# ---------------- Utilities ----------------
def repo_name_from_url(repo_url: str) -> str:
    name = urlparse(repo_url).path.rstrip("/").split("/")[-1]
    return name[:-4] if name.endswith(".git") else name

def load_report(path: str):
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"[error] env report not found: {path}")
    try:
        return json.loads(p.read_text())
    except Exception as e:
        raise SystemExit(f"[error] failed to parse report JSON: {e}")

def prefer_port(ports):
    if not ports:
        return 8000
    try:
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
    except Exception:
        return 8000

def heuristic_machine_and_disk(report: dict):
    """
    CPU-only sizing for GCP:
      - hello-world: e2-micro, 20GB
      - Flask/Django/Express: e2-small, 30GB
      - heavy deps (pandas/scikit/opencv/playwright): e2-standard-2, 50GB
      - ML deps (torch/tensorflow): e2-standard-4, 80GB
    """
    def to_str(x):
        try:
            return str(x).lower()
        except Exception:
            return ""

    dep_names = []
    for d in (report.get("dependencies") or []):
        name = d.get("name") if isinstance(d, dict) else d
        if name:
            dep_names.append(to_str(name))

    frameworks = " ".join([to_str(f) for f in (report.get("frameworks") or [])])
    deps = " ".join(dep_names)
    lang = to_str(report.get("language", ""))

    mtype, disk = "e2-micro", 20
    heavy = any(k in deps for k in ["pandas", "scikit", "opencv", "playwright", "chromium", "bun", "gradle"])
    ml    = any(k in deps for k in ["torch", "tensorflow", "jax", "transformers", "xgboost", "lightgbm", "cuda"])
    nodeish = ("node" in lang) or any(k in deps for k in ["next", "nuxt", "nest", "vite"])

    if ml:
        mtype, disk = "e2-standard-4", 80
    elif heavy:
        mtype, disk = "e2-standard-2", 50
    elif nodeish:
        mtype, disk = "e2-small", 30
    elif any(f in frameworks for f in ["django", "flask", "fastapi", "express", "rails", "spring"]):
        mtype, disk = "e2-small", 30

    return mtype, disk


def prompt_for_api_key_once():
    """
    If neither OPENAI_API_KEY nor OPENROUTER_API_KEY is set, prompt user once.
    Returns True if a key was provided (and env set), False if user refused.
    """
    if os.getenv("OPENAI_API_KEY") or os.getenv("OPENROUTER_API_KEY"):
        return True

    print("\n[info] No AI API key found (OPENAI_API_KEY or OPENROUTER_API_KEY).")
    choice = input("Enter an API key (or 'no' to skip AI sizing): ").strip()
    if not choice or choice.lower() == "no":
        print("[info] Skipping AI sizing → using heuristics.")
        return False

    # Simple provider inference by prefix
    if choice.startswith("sk-or-"):
        os.environ["OPENROUTER_API_KEY"] = choice
        os.environ["AI_PROVIDER"] = "openrouter"
    else:
        os.environ["OPENAI_API_KEY"] = choice
        os.environ["AI_PROVIDER"] = os.getenv("AI_PROVIDER") or "openai"
    return True


def ai_suggest_machine_and_disk(prompt: str, report: dict):
    """
    Use AI to pick machine_type and disk_size_gb for GCP.
    - If no key is set, prompt user. If they refuse → heuristics.
    - If request fails (e.g., 401), ask once more for a key; if refused → heuristics.
    """
    if not prompt_for_api_key_once():
        return heuristic_machine_and_disk(report)

    def try_once():
        content = chat_complete(
            [
                {"role": "system", "content": "You choose GCP VM sizing. Output JSON only."},
                {"role": "user", "content": f"""
Prompt: {prompt}

Repo environment (truncated):
{json.dumps({
    "language": report.get("language"),
    "frameworks": report.get("frameworks"),
    "dependencies": [(d.get("name") if isinstance(d, dict) else d) for d in (report.get("dependencies") or [])][:50],
    "notes": report.get("notes"),
}, ensure_ascii=False)}

Return strict JSON:
{{
  "machine_type": "e2-micro|e2-small|e2-medium|e2-standard-2|e2-standard-4",
  "disk_size_gb": <int 10..200>
}}
""".strip()}
            ],
        )
        jc = json.loads(content)
        mt = jc.get("machine_type") or "e2-small"
        ds = int(jc.get("disk_size_gb") or 30)
        if mt not in {"e2-micro","e2-small","e2-medium","e2-standard-2","e2-standard-4"}:
            mt = "e2-small"
        if ds < 10 or ds > 200:
            ds = 30
        print(f"[info] AI sizing: {mt}, {ds}GB")
        return mt, ds

    try:
        return try_once()
    except Exception as e:
        print(f"[warn] AI sizing failed: {e}")
        # Give the user one more chance to enter/replace a key
        print("\nYou can re-enter an API key or type 'no' to skip.")
        # Clear any previous (possibly bad) and keys to allow switching providers
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("OPENROUTER_API_KEY", None)
        if not prompt_for_api_key_once():
            return heuristic_machine_and_disk(report)
        try:
            return try_once()
        except Exception as e2:
            print(f"[warn] AI sizing failed again: {e2}. Using heuristics.")
            return heuristic_machine_and_disk(report)


# ---------------- Inference from report ----------------
def infer_config(prompt: str, repo_url: str, report: dict, cli):
    # Force cloud = gcp, style = vm
    cfg = {
        "cloud": "gcp",
        "style": "vm",
        "project": cli.project,
        "region": cli.region or "us-central1",
        "zone": cli.zone or "us-central1-a",
        "repo_url": repo_url,
        "app_port": 8000,
        "gcr_image": report.get("gcr_image"),
        "start_command": "",
        "env_lines": [],
        "machine_type": cli.machine_type,   # may be None → fill later
        "disk_size_gb": int(cli.disk_size) if cli.disk_size else None,
        "image": "ubuntu-os-cloud/ubuntu-2204-lts",
    }
    
    # From report
    if report.get("ports"):
        cfg["app_port"] = prefer_port(report["ports"])
    starts = report.get("start_commands") or []
    if starts:
        cfg["start_command"] = str(starts[0]).strip()
    for e in (report.get("env_vars") or []):
        n, d = e.get("name"), e.get("default")
        if n and d not in (None, ""):
            cfg["env_lines"].append(f"{n}={d}")

    # Ensure HOST/PORT
    if not any(line.startswith("PORT=") for line in cfg["env_lines"]):
        cfg["env_lines"].append(f"PORT={cfg['app_port']}")
    if not any(line.startswith("HOST=") for line in cfg["env_lines"]):
        cfg["env_lines"].append("HOST=0.0.0.0")

    # Fallback start command by language
    if not cfg["start_command"]:
        lang = (report.get("language") or "").lower()
        cfg["start_command"] = "npm start" if lang.startswith("node") else "python app.py"

    # Size the VM (CLI override > AI > heuristic)
    if cfg["machine_type"] and cfg["disk_size_gb"] is not None:
        pass
    else:
        mt, ds = ai_suggest_machine_and_disk(prompt, report)
        cfg["machine_type"] = cfg["machine_type"] or mt
        cfg["disk_size_gb"] = cfg["disk_size_gb"] or int(ds)

    return cfg


def write_file(path: Path, content: str):
    path.write_text(content.strip() + "\n")


# ---------------- Writers: startup + Terraform ----------------
# The write_startup function from write_tf_gcp_from_report.py
def write_startup(outdir: Path, cfg: dict):
    app_dir_default = os.getenv("API_DIR") or os.getenv("APP_DIR") or "/opt/app"
    env_lines = "\\n".join(cfg["env_lines"])
    
    # Use the gcr image name that was pushed locally
    gcr_image_name = cfg['gcr_image']
    app_port = cfg['app_port']
    
    # This script does not clone a repo or build a docker image
    startup = f"""#!/bin/bash
set -euxo pipefail

mkdir -p /var/log/autodeploy
exec > >(tee -a /var/log/autodeploy/startup.log) 2>&1

# Install Docker
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y docker.io
fi
systemctl enable --now docker || true

# Pull the pre-built image
docker pull {gcr_image_name}

# Create a simple .env file on the VM
cat > /opt/app.env <<'EOF'
{os.linesep.join(cfg['env_lines'])}
EOF

# Run the container
docker rm -f autodeploy-app || true
docker run -d --name autodeploy-app --env-file /opt/app.env -p {app_port}:{app_port} {gcr_image_name}

# Forward port 80 to the application port
if command -v iptables >/dev/null 2>&1; then
  iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-ports {app_port}
  iptables -t nat -A OUTPUT -p tcp --dport 80 -j REDIRECT --to-ports {app_port}
else
  echo "[warn] iptables not present; port 80 will not forward to {app_port}."
fi
"""
    write_file(outdir / "startup.sh", startup)



def write_main_tf(outdir: Path, cfg: dict):
    # This is a regular multi-line string, not an f-string, to avoid syntax errors with Terraform's braces.
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

# Allow HTTP/80 (for Docker -p 80:PORT or NAT)
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

# Also allow the app's actual port (e.g., 5000/8000) so it's reachable even without NAT
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

# Use a data source to get the default service account's email and break the dependency cycle.
data "google_compute_default_service_account" "default" {
  project = var.project
}

# Grant the VM service account permissions to pull from GCR/Artifact Registry
resource "google_project_iam_member" "gcr_reader" {
  project = var.project
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${data.google_compute_default_service_account.default.email}"
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
    network     = "default"
    access_config {}
  }

  tags = ["http-server"]

  metadata_startup_script = file("startup.sh")

  service_account {
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }

  depends_on = [
    google_compute_firewall.http,
    google_compute_firewall.app,
    google_project_iam_member.gcr_reader
  ]
}
"""
    write_file(outdir / "main.tf", main_tf)








def write_variables_tf(outdir: Path):
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
"""
    write_file(outdir / "variables.tf", variables_tf)



def write_outputs_tf(outdir: Path):
    outputs_tf = textwrap.dedent("""\
    output "public_ip" {
      description = "Public IP of the VM"
      value       = google_compute_instance.app.network_interface[0].access_config[0].nat_ip
    }
    """)
    write_file(outdir / "outputs.tf", outputs_tf)


def write_tfvars(outdir: Path, cfg: dict):
    tfvars = {
        "project": cfg["project"],
        "region": cfg["region"],
        "zone": cfg["zone"],
        "machine_type": cfg["machine_type"],
        "disk_size_gb": cfg["disk_size_gb"],
        "image": cfg["image"],
        "app_port": cfg["app_port"],  # ensures :<app_port> is also reachable
    }
    (outdir / "terraform.tfvars.json").write_text(json.dumps(tfvars, indent=2) + "\n")



# ---------------- CLI ----------------
def main():
    ap = argparse.ArgumentParser(description="Generate Terraform GCP (VM) bundle from analyzer report")
    ap.add_argument("--prompt", required=True, help="Natural language deployment request")
    ap.add_argument("--repo", required=True, help="GitHub repo URL (public for MVP)")
    ap.add_argument("--report", required=True, help="Path to env_report.json from analyze_repo_env.py")
    ap.add_argument("--project", required=True, help="GCP project ID")
    ap.add_argument("--region", default=None, help="GCP region (default: us-central1)")
    ap.add_argument("--zone", default=None, help="GCP zone (default: us-central1-a)")
    ap.add_argument("--machine-type", dest="machine_type", default=None, help="Override VM type (e.g., e2-small)")
    ap.add_argument("--disk-size", dest="disk_size", default=None, help="Override disk size GB (int)")
    args = ap.parse_args()

    report = load_report(args.report)
    cfg = infer_config(args.prompt, args.repo, report, args)

    outdir = Path(f"tf_out_{repo_name_from_url(args.repo)}")
    outdir.mkdir(parents=True, exist_ok=True)

    write_startup(outdir, cfg)
    write_main_tf(outdir, cfg)
    write_variables_tf(outdir)
    write_outputs_tf(outdir)
    write_tfvars(outdir, cfg)

    print("\n[ok] Terraform bundle (GCP VM) written to:", str(outdir))
    print("\nNext steps:")
    print(f"  cd {outdir}")
    print("  terraform init")
    print("  terraform apply -auto-approve")
    print("\nAfter apply, open:  http://<public_ip>/")
    print("\nNotes:")
    print("- Enable APIs if needed:")
    print("    gcloud services enable compute.googleapis.com --project", cfg["project"])
    print("- Auth setup (one of):")
    print("    gcloud auth application-default login")
    print("    # or set GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json")
    print("- Firewall opens port 80 to the world (MVP). Tighten for prod.")
    print("- Startup prefers Docker if Dockerfile exists; else Python/systemd fallback.")
    print("- To influence default app dir baked into startup, set API_DIR or APP_DIR before generation.")

if __name__ == "__main__":
    main()