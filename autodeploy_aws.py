#!/usr/bin/env python3
# -*- coding: utf-8 -*-



"""
autodeploy_chat.py

A unified script to automate application deployment using an LLM to orchestrate
the entire process from natural language input to cloud provisioning via Terraform.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import random
import string
import requests
from pathlib import Path
from urllib.parse import urlparse

os.environ['OPENROUTER_API_KEY'] = ''
os.environ["GCP_BILLING_ACCOUNT_ID"] = ''

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
            "X-Title": "AutoDeploy Chat System",
        }
        payload = {"model": model, "messages": messages, "temperature": 0}
    else: # Default to OpenAI
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        url = "https://api.openai.com/v1/chat/completions"
        model = model or os.getenv("AI_MODEL") or "gpt-4o-mini"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": messages, "temperature": 0}

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except requests.exceptions.HTTPError as err:
        print(f"HTTP error occurred: {err.response.status_code} - {err.response.text}", file=sys.stderr)
        raise
    except Exception as err:
        print(f"An unexpected error occurred: {err}", file=sys.stderr)
        raise

# ---------------- Generic helpers ----------------
def safe_input(prompt: str, default: str | None = None) -> str:
    s = input(f"{prompt}{' ['+default+']' if default else ''}: ").strip()
    return s or (default or "")

def repo_name_from_url(repo_url: str) -> str:
    name = urlparse(repo_url).path.rstrip("/").split("/")[-1]
    return name[:-4] if name.endswith(".git") else name


def get_repo_tree(owner, repo, branch="main"):
    api_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "AutoDeploy Chat System"}
    github_pat = os.getenv('GITHUB_PAT')
    if github_pat:
        headers["Authorization"] = f"token {github_pat}"
    try:
        response = requests.get(api_url, headers=headers)
        response.raise_for_status()
        tree = response.json().get('tree', [])
        return [item['path'] for item in tree if item['type'] == 'blob']
    except requests.exceptions.RequestException as e:
        print(f"Error accessing repo tree: {e}")
        return None

def write_file(path: Path, content: str):
    path.write_text(content.rstrip() + "\n")

# --- Terraform and Startup Script Generation ---
def create_startup_sh(repo_url: str, app_port: int, entrypoint: str, dependencies: str) -> str:
    return f"""#!/usr/bin/env bash
set -euxo pipefail

# ---- logging ----
LOG_DIR=/var/log/autodeploy
LOG_FILE="$LOG_DIR/startup.log"
mkdir -p "$LOG_DIR"
touch "$LOG_FILE"
chmod 0644 "$LOG_FILE"
# send all stdout/stderr to the log file
exec >>"$LOG_FILE" 2>&1

echo "=== $(date -Is) startup.sh BEGIN ==="

REPO_URL="{repo_url}"
REPO_DIR="/opt/app"

# ---- packages ----
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git python3 python3-pip

# ---- app setup ----
mkdir -p "$REPO_DIR"
if [ ! -d "$REPO_DIR/.git" ]; then
  git clone --depth 1 "$REPO_URL" "$REPO_DIR"
else
  git -C "$REPO_DIR" pull --ff-only || true
fi

cd "$REPO_DIR"
if [ -f "{dependencies}" ]; then
  pip3 install -r {dependencies}
fi

# ---- run app ----
nohup python3 {entrypoint} >/dev/null 2>&1 &
echo "=== $(date -Is) startup.sh END ==="
"""


def write_terraform_files(provider: str, app_port: int, repo_name: str, output_dir: Path, project_id: str=None):
    """
    provider: "GCP" or "Azure"
      - For GCP: project_id = GCP project ID
      - For Azure: ignores project_id; reads AZURE_* env vars instead
    """
    if provider == "GCP":
        main_tf = """
terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }
}

provider "google" {
  project = var.project
  region  = var.region
}

resource "google_compute_firewall" "http_traffic" {
  name    = "autodeploy-http"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["80"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["http-server"]
}

resource "google_compute_firewall" "app_traffic" {
  name    = "autodeploy-app"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = [tostring(var.app_port)]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["http-server"]
}

resource "google_compute_instance" "app_vm" {
  name         = "autodeploy-vm"
  machine_type = var.vm_size
  zone         = var.zone

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2204-lts"
    }
  }

  network_interface {
    network = "default"
    access_config {}
  }

  metadata_startup_script = file("startup.sh")
  tags = ["http-server"]
}

output "public_ip" {
  value = google_compute_instance.app_vm.network_interface[0].access_config[0].nat_ip
}
""".lstrip()

        variables_tf = """
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

variable "vm_size" {
  description = "GCP VM size"
  type        = string
  default     = "e2-small"
}

variable "app_port" {
  description = "Application port"
  type        = number
}
""".lstrip()

        tfvars_data = {"app_port": app_port, "project": project_id, "zone": "us-central1-a"}

        write_file(output_dir / "main.tf", main_tf)
        write_file(output_dir / "variables.tf", variables_tf)
        write_file(output_dir / "terraform.tfvars.json", json.dumps(tfvars_data, indent=2))
        print(f"✅ Terraform files generated for {provider}.")

    elif provider == "Azure":
        # Gather config from env (with sane defaults)
        subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID")
        if not subscription_id:
            # best-effort auto-detect via CLI
            try:
                res = subprocess.run(
                    ["az", "account", "show", "--query", "id", "-o", "tsv"],
                    check=True, capture_output=True, text=True
                )
                subscription_id = res.stdout.strip()
            except Exception:
                raise RuntimeError("AZURE_SUBSCRIPTION_ID is not set and could not auto-detect via `az account show`.")

        location       = os.getenv("AZURE_LOCATION", "eastus")
        rg_name        = os.getenv("AZURE_RG_NAME", "autodeploy-rg")
        vm_size        = os.getenv("AZURE_VM_SIZE", "Standard_B2s")
        admin_username = os.getenv("AZURE_ADMIN_USERNAME", "azureuser")

        ssh_pub = os.getenv("AZURE_SSH_PUBLIC_KEY")
        if not ssh_pub:
            # Try common keys
            for p in [Path.home()/".ssh/id_ed25519.pub", Path.home()/".ssh/id_rsa.pub"]:
                if p.exists():
                    ssh_pub = p.read_text().strip()
                    break
        if not ssh_pub:
            raise RuntimeError("Provide an SSH public key via AZURE_SSH_PUBLIC_KEY or at ~/.ssh/id_ed25519.pub / ~/.ssh/id_rsa.pub")

        main_tf = f"""
terraform {{
  required_providers {{
    azurerm = {{
      source  = "hashicorp/azurerm"
      version = ">= 3.100.0"
    }}
  }}
}}

provider "azurerm" {{
  features {{}}
  subscription_id = var.subscription_id
}}

resource "azurerm_resource_group" "rg" {{
  name     = var.rg_name
  location = var.location
}}

resource "azurerm_virtual_network" "vnet" {{
  name                = "autodeploy-vnet"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  address_space       = ["10.0.0.0/16"]
}}

resource "azurerm_subnet" "subnet" {{
  name                 = "autodeploy-subnet"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = ["10.0.1.0/24"]
}}

resource "azurerm_network_security_group" "nsg" {{
  name                = "autodeploy-nsg"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name

  security_rule {{
    name                       = "allow-ssh"
    priority                   = 1000
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }}

  security_rule {{
    name                       = "allow-http"
    priority                   = 1001
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "80"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }}

  security_rule {{
    name                       = "allow-app"
    priority                   = 1002
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = tostring(var.app_port)
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }}
}}

resource "azurerm_public_ip" "app_pip" {{
  name                = "autodeploy-pip"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  allocation_method   = "Static"
  sku                 = "Standard"
}}

resource "azurerm_network_interface" "nic" {{
  name                = "autodeploy-nic"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name

  ip_configuration {{
    name                          = "internal"
    subnet_id                     = azurerm_subnet.subnet.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.app_pip.id
  }}
}}

resource "azurerm_network_interface_security_group_association" "nic_nsg" {{
  network_interface_id      = azurerm_network_interface.nic.id
  network_security_group_id = azurerm_network_security_group.nsg.id
}}

resource "azurerm_linux_virtual_machine" "app_vm" {{
  name                = "autodeploy-vm"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  size                = var.vm_size
  admin_username      = var.admin_username

  network_interface_ids = [
    azurerm_network_interface.nic.id
  ]

  os_disk {{
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
  }}

  # Ubuntu 22.04 LTS Gen2
  source_image_reference {{
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts-gen2"
    version   = "latest"
  }}

  disable_password_authentication = true
  admin_ssh_key {{
    username   = var.admin_username
    public_key = var.ssh_public_key
  }}

  # Inject our startup.sh (cloud-init)
  custom_data = filebase64("startup.sh")
}}

output "public_ip" {{
  value = azurerm_public_ip.app_pip.ip_address
}}
""".lstrip()

        variables_tf = """
variable "subscription_id" {
  description = "Azure Subscription ID"
  type        = string
}

variable "location" {
  description = "Azure location/region"
  type        = string
  default     = "eastus"
}

variable "rg_name" {
  description = "Resource Group name"
  type        = string
  default     = "autodeploy-rg"
}

variable "vm_size" {
  description = "Azure VM size"
  type        = string
  default     = "Standard_B2s"
}

variable "admin_username" {
  description = "Admin username for the VM"
  type        = string
  default     = "azureuser"
}

variable "ssh_public_key" {
  description = "SSH public key for the admin user"
  type        = string
}

variable "app_port" {
  description = "Application port"
  type        = number
}
""".lstrip()

        tfvars_data = {
            "subscription_id": subscription_id,
            "location":        location,
            "rg_name":         rg_name,
            "vm_size":         vm_size,
            "admin_username":  admin_username,
            "ssh_public_key":  ssh_pub,
            "app_port":        app_port,
        }

        write_file(output_dir / "main.tf", main_tf)
        write_file(output_dir / "variables.tf", variables_tf)
        write_file(output_dir / "terraform.tfvars.json", json.dumps(tfvars_data, indent=2))
        print(f"✅ Terraform files generated for {provider}.")

    else:
        print(f"❌ Terraform file generation for {provider} not implemented.")

def write_terraform_files_aws(app_port: int, repo_name: str, output_dir: Path):
    """
    Writes Terraform for AWS (EC2 in default VPC, SG opening 22/80/app_port),
    key pair from provided SSH public key, Ubuntu 22.04, user_data=startup.sh.

    Reads:
      - AWS_REGION (default us-east-1)
      - AWS_INSTANCE_TYPE (default t3.small)  # typically set by LLM in main()
      - AWS_SSH_PUBLIC_KEY (falls back to ~/.ssh/id_ed25519.pub or id_rsa.pub)
    """
    region        = os.getenv("AWS_REGION", "us-east-1")
    instance_type = os.getenv("AWS_INSTANCE_TYPE", "t3.small")

    ssh_pub = os.getenv("AWS_SSH_PUBLIC_KEY")
    if not ssh_pub:
        for p in [Path.home()/".ssh/id_ed25519.pub", Path.home()/".ssh/id_rsa.pub"]:
            if p.exists():
                ssh_pub = p.read_text().strip()
                break
    if not ssh_pub:
        raise RuntimeError("Provide an SSH public key via AWS_SSH_PUBLIC_KEY or at ~/.ssh/id_ed25519.pub / ~/.ssh/id_rsa.pub")

    main_tf = """
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

# Default VPC + its subnets
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# What AZs are available in this account/region?
data "aws_availability_zones" "available" {
  state = "available"
}

# Which AZs offer the chosen instance type?
data "aws_ec2_instance_type_offerings" "it" {
  location_type = "availability-zone"

  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }

  filter {
    name   = "location"
    values = data.aws_availability_zones.available.names
  }
}

# Load per-subnet details so we can match AZs
data "aws_subnet" "details" {
  for_each = toset(data.aws_subnets.default.ids)
  id       = each.value
}

locals {
  # AZs that offer this instance type
  allowed_azs = toset(data.aws_ec2_instance_type_offerings.it.locations)

  # Subnets whose AZ supports the instance type
  matching_subnets = [
    for s in data.aws_subnet.details :
    s.id if contains(local.allowed_azs, s.availability_zone)
  ]

  # Choose a good subnet; if none match (rare), fallback to first default subnet
  chosen_subnet_id = length(local.matching_subnets) > 0 ? local.matching_subnets[0] : tolist(data.aws_subnets.default.ids)[0]

  # Pick AMI arch based on instance family (t4g.* = arm64). Use startswith to avoid regex escaping issues.
  is_arm = startswith(var.instance_type, "t4g.")
}

# AMIs for Ubuntu 22.04 (Jammy), both arches
data "aws_ami" "ubuntu_amd64" {
  owners      = ["099720109477"] # Canonical
  most_recent = true
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

data "aws_ami" "ubuntu_arm64" {
  owners      = ["099720109477"] # Canonical
  most_recent = true
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-arm64-server-*"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

locals {
  ami_id = local.is_arm ? data.aws_ami.ubuntu_arm64.id : data.aws_ami.ubuntu_amd64.id
}

# Security group opening SSH, HTTP, and the app port
resource "aws_security_group" "app_sg" {
  name        = "autodeploy-sg"
  description = "Allow SSH, HTTP, and app port"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = var.app_port
    to_port     = var.app_port
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Import an SSH key from the provided public key
resource "aws_key_pair" "autodeploy" {
  key_name   = "autodeploy-key"
  public_key = var.ssh_public_key
}

resource "aws_instance" "app" {
  ami                         = local.ami_id
  instance_type               = var.instance_type
  subnet_id                   = local.chosen_subnet_id
  vpc_security_group_ids      = [aws_security_group.app_sg.id]
  key_name                    = aws_key_pair.autodeploy.key_name
  associate_public_ip_address = true
  user_data                   = file("startup.sh")

  tags = {
    Name = "autodeploy-vm"
  }
}

output "public_ip" {
  value = aws_instance.app.public_ip
}
""".lstrip()

    variables_tf = """
variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "EC2 instance type (LLM-selected or env-provided)"
  type        = string
  default     = "t3.small"
}

variable "ssh_public_key" {
  description = "SSH public key for the EC2 key pair"
  type        = string
}

variable "app_port" {
  description = "Application port"
  type        = number
}
""".lstrip()

    tfvars_data = {
        "region":         region,
        "instance_type":  instance_type,
        "ssh_public_key": ssh_pub,
        "app_port":       app_port,
    }

    write_file(output_dir / "main.tf", main_tf)
    write_file(output_dir / "variables.tf", variables_tf)
    write_file(output_dir / "terraform.tfvars.json", json.dumps(tfvars_data, indent=2))
    print("✅ Terraform files generated for AWS (AZ-aware subnet + arch-aware AMI, no regex escapes).")






def pick_random_existing_project(billing_account_id: str | None) -> str | None:
    """
    Returns a usable existing projectId at random, or None if none work.
    A project is considered usable if:
      - we can `gcloud config set project <id>`
      - billing is linked (we try to link; it's fine if already linked)
      - Compute Engine API can be enabled
    """
    # Get active projects the caller can see
    lst = subprocess.run(
        ["gcloud", "projects", "list", "--filter=lifecycleState=ACTIVE", "--format=value(projectId)"],
        check=False, capture_output=True, text=True
    )
    projects = [p.strip() for p in (lst.stdout or "").splitlines() if p.strip()]
    if not projects:
        return None

    random.shuffle(projects)

    for pid in projects:
        try:
            # Point gcloud at the candidate
            subprocess.run(["gcloud", "config", "set", "project", pid], check=True, capture_output=True, text=True)

            # Try to link billing (ignore failure if already linked or permission-limited)
            if billing_account_id:
                subprocess.run(
                    ["gcloud", "billing", "projects", "link", pid, "--billing-account", billing_account_id],
                    check=False, capture_output=True, text=True
                )

            # Ensure Compute Engine API is on
            subprocess.run(["gcloud", "services", "enable", "compute.googleapis.com"],
                           check=True, capture_output=True, text=True)

            return pid  # success
        except subprocess.CalledProcessError:
            # Try next candidate
            continue

    return None


def purge_non_aws_tf_files(output_dir: Path):
    """Delete any *.tf file in output_dir that mentions azurerm/google providers."""
    for p in output_dir.glob("*.tf"):
        try:
            txt = p.read_text()
        except Exception:
            continue
        if "azurerm" in txt or 'provider "azurerm"' in txt or "google" in txt or 'provider "google"' in txt:
            print(f"🧹 Removing leftover non-AWS file: {p.name}")
            try:
                p.unlink()
            except Exception:
                pass

def backup_and_remove_state_if_non_aws(output_dir: Path):
    """If terraform.tfstate contains any non-AWS resources, back it up and remove it."""
    sp = output_dir / "terraform.tfstate"
    if not sp.exists():
        return
    try:
        data = json.loads(sp.read_text())
    except Exception:
        # if unreadable, be safe and move aside
        new = output_dir / f"terraform.tfstate.backup"
        print(f"🧳 Backing up unreadable state -> {new.name}")
        sp.replace(new)
        return
    # scan resource types; AWS resources have types like "aws_instance", "aws_security_group", etc.
    non_aws = []
    for res in (data.get("resources") or []):
        t = res.get("type", "")
        if not t.startswith("aws_"):
            non_aws.append(t)
    if non_aws:
        new = output_dir / "terraform.tfstate.azure_or_gcp.backup"
        print(f"🧳 Found non-AWS resources in state {set(non_aws)}; backing up -> {new.name}")
        sp.replace(new)


def choose_aws_instance_type(app_type: str | None, region: str) -> str:
    """
    Ask the LLM to recommend a widely-available AWS instance type for our app.
    Returns a string like 't3.small' with a conservative fallback.
    """
    system = (
        "You are a cloud sizing assistant. Pick a broadly available AWS EC2 instance type "
        "that is cost-effective for a small web app (CPU only). Prefer t3 for x86, or t4g if "
        "the user explicitly targets ARM. Avoid GPU. Output ONLY JSON: {\"instance_type\":\"...\"}."
    )
    user = json.dumps({
        "app_type": app_type or "generic web app",
        "region": region
    })

    try:
        ans = chat_complete(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=None, provider=None, timeout=45
        )
        if ans and ans.strip().startswith("{"):
            picked = json.loads(ans).get("instance_type", "").strip()
            # Basic sanity
            if re.fullmatch(r"[a-z0-9]+\.[a-z0-9]+", picked):
                return picked
    except Exception:
        pass

    # Safe fallback if LLM call fails or returns junk
    return "t3.small"


def main():
    print("=== Autodeploy Chat System: Full Deployment Workflow ===\n")

    # --- 1) User Input & LLM Intent Parsing ---
    user_prompt = safe_input("Describe your deployment (e.g., 'Deploy my Flask app on GCP')")
    repo_url = safe_input("GitHub repo URL", "https://github.com/Arvo-AI/hello_world")
    repo_name = repo_name_from_url(repo_url)
    owner = urlparse(repo_url).path.split('/')[1]

    system_message_intent = """
    You are a highly specialized AI assistant for a cloud deployment system. Your task is to extract and **normalize** key information from a user's request.

    **Rules:**
    - Identify the target **cloud provider**. Correct any typos or abbreviations to the full, standardized name (e.g., 'AWS', 'GCP', 'Azure').
    - Identify the application **framework or type**. Correct any typos or abbreviations to the full, standardized name (e.g., 'Flask', 'Django', 'Node.js', 'Java').
    - If a specific cloud provider or app type is not mentioned, return `null`.

    **Response Format:**
    Respond with a single JSON object with these two keys: `cloud_provider` and `app_type`. No extra text or formatting.
    """
    messages_intent = [
        {"role": "system", "content": system_message_intent},
        {"role": "user", "content": user_prompt},
    ]

    print("Analyzing user request...")
    llm_response_intent = chat_complete(messages_intent)

    if not llm_response_intent or not llm_response_intent.strip().startswith('{'):
        print(f"❌ LLM returned an empty or invalid response. Response was: '{llm_response_intent}'")
        sys.exit(1)

    extracted_info = json.loads(llm_response_intent)
    cloud_provider = extracted_info.get('cloud_provider')
    app_type = extracted_info.get('app_type')
    print(f"✅ Intent parsed. Cloud Provider: {cloud_provider}, App Type: {app_type}")

    # --- 2) LLM-Driven Repository Analysis ---
    all_file_paths = get_repo_tree(owner, repo_name)
    if not all_file_paths:
        print("❌ Could not retrieve repository file list. Aborting.")
        sys.exit(1)

    system_message_files = """
    You are a highly specialized AI assistant for analyzing application repositories. The user will provide a list of all file paths in a repository.

    Your task is to identify the correct file path for each of the following:
    1. The primary dependency file (e.g., 'requirements.txt', 'package.json', 'pom.xml').
    2. The primary Dockerfile (e.g., 'Dockerfile').
    3. The primary application entry point (e.g., 'app.py', 'server.js').

    Return a single JSON object where keys are standardized file names ('Dockerfile', 'dependencies', 'entrypoint') and values are the correct file paths. If a file is not found, its value should be `null`.

    Your response must be a valid, minified JSON object with no additional text or formatting.
    """
    messages_files = [
        {"role": "system", "content": system_message_files},
        {"role": "user", "content": json.dumps(all_file_paths)},
    ]

    print("Analyzing repository file structure...")
    llm_response_files = chat_complete(messages_files)
    extracted_files = json.loads(llm_response_files)
    print("✅ Repository files analyzed.")

    # --- 3) Generate Startup Script via LLM and Write TF Bundle ---
    output_dir = Path(f"./tf_out_{repo_name}")
    output_dir.mkdir(parents=True, exist_ok=True)

    startup_script_prompt = f"""
    You are a specialized AI assistant for generating shell scripts to deploy applications on a clean Ubuntu VM. Here is a summary of the repository's key files: {json.dumps(extracted_files)}.

    Your task is to generate a 'startup.sh' script that will:
    1.  Clone the repository from GitHub: {repo_url}.
    2.  Install the necessary language runtime and package manager (e.g., Python and pip, Node.js and npm).
    3.  Install the application's dependencies.
    4.  Run the application with the correct start command.
    5.  Set any environment variables that are needed.
    6.  Ensure the script is self-contained and runnable.

    Respond with a single JSON object containing two keys:
    1.  'startup_script': The full, plain-text content of the startup.sh script.
    2.  'app_port': The most likely port the application runs on (e.g., 5000, 8000).

    Your response must be a valid, minified JSON object with no additional text or formatting.
    """
    messages_startup = [
        {"role": "system", "content": startup_script_prompt},
        {"role": "user", "content": json.dumps(extracted_files)},
    ]

    print("Generating startup script...")
    llm_response_startup = chat_complete(messages_startup)
    generated_config = json.loads(llm_response_startup)
    startup_script = generated_config["startup_script"]
    app_port = generated_config["app_port"]

    startup_path = output_dir / "startup.sh"
    startup_path.write_text(startup_script)
    os.chmod(startup_path, 0o755)
    print(f"✅ Startup script generated and saved to {startup_path}.")

    # --- 4) Dynamic Provisioning & Deployment ---
    # GCP path
    if cloud_provider and re.search(r'\b(gcp|google\s+cloud|google\s+cloud\s+platform)\b', cloud_provider, flags=re.I):
        billing_account_id = os.getenv("GCP_BILLING_ACCOUNT_ID")
        if not billing_account_id:
            print("❌ Billing account ID not provided. Aborting.")
            sys.exit(1)

        # Try to create a new project first; on quota, reuse a random ACTIVE project.
        new_project_id = "autodeploy-proj-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        effective_project_id = new_project_id

        print(f"--- Creating New GCP Project: {new_project_id} ---")
        print(f"$ gcloud projects create {new_project_id}")
        create = subprocess.run(
            ["gcloud", "projects", "create", new_project_id],
            check=False, text=True, capture_output=True
        )

        if create.returncode != 0:
            combined = f"{create.stderr or ''}\n{create.stdout or ''}".lower()
            if ("exceeded your allotted project quota" in combined) or ("quotafailure" in combined) or ("quota" in combined):
                print("⚠️ Project quota exceeded. Attempting to use a random existing ACTIVE project...")
                print("$ gcloud projects list --filter=lifecycleState=ACTIVE --format=value(projectId)")
                lst = subprocess.run(
                    ["gcloud", "projects", "list", "--filter=lifecycleState=ACTIVE", "--format=value(projectId)"],
                    check=False, text=True, capture_output=True
                )
                candidates = [p.strip() for p in (lst.stdout or "").splitlines() if p.strip()]
                if not candidates:
                    print("❌ No ACTIVE projects available to reuse. Aborting.")
                    sys.exit(1)

                random.shuffle(candidates)
                prepared = None
                for pid in candidates:
                    try:
                        print(f"$ gcloud config set project {pid}")
                        subprocess.run(["gcloud", "config", "set", "project", pid], check=True)

                        if billing_account_id:
                            print(f"$ gcloud billing projects link {pid} --billing-account {billing_account_id}")
                            subprocess.run(
                                ["gcloud", "billing", "projects", "link", pid, "--billing-account", billing_account_id],
                                check=False
                            )

                        print("$ gcloud services enable compute.googleapis.com")
                        subprocess.run(["gcloud", "services", "enable", "compute.googleapis.com"], check=True)

                        prepared = pid
                        break
                    except subprocess.CalledProcessError:
                        print(f"↪️  Skipping '{pid}' (failed to prepare). Trying another...")
                        continue

                if prepared:
                    effective_project_id = prepared
                    print(f"✅ Using existing project '{effective_project_id}'.")
                else:
                    print("❌ Could not prepare any existing project (billing/API/permissions). Aborting.")
                    sys.exit(1)
            else:
                details = (create.stderr or "") + ("\n" + create.stdout if create.stdout else "")
                print(f"❌ Failed to create GCP project. Details:\n{details}")
                sys.exit(1)
        else:
            print(f"✅ Project '{new_project_id}' created.")
            try:
                print(f"$ gcloud billing projects link {new_project_id} --billing-account {billing_account_id}")
                subprocess.run(
                    ["gcloud", "billing", "projects", "link", new_project_id, "--billing-account", billing_account_id],
                    check=True
                )
                print("✅ Project linked to billing account.")

                print(f"$ gcloud config set project {new_project_id}")
                subprocess.run(["gcloud", "config", "set", "project", new_project_id], check=True)
                print(f"✅ gcloud project set to '{new_project_id}'.")

                print("$ gcloud services enable compute.googleapis.com")
                subprocess.run(["gcloud", "services", "enable", "compute.googleapis.com"], check=True)
                print("✅ Compute Engine API enabled.")
            except subprocess.CalledProcessError:
                print("❌ Failed to configure new GCP project (see errors above).")
                sys.exit(1)

        # Write TF (GCP) and apply
        write_terraform_files("GCP", app_port, repo_name, output_dir, project_id=effective_project_id)

        print("\n--- Executing Terraform commands (GCP) ---")
        try:
            subprocess.run(["terraform", "init", "-reconfigure"], cwd=output_dir, check=True)
            print("✅ Terraform init successful.")
            subprocess.run(["terraform", "apply", "-auto-approve", "-input=false"], cwd=output_dir, check=True)
            print("✅ Terraform apply successful.")

            result = subprocess.run(
                ["terraform", "output", "-json", "public_ip"],
                cwd=output_dir, check=True, capture_output=True, text=True
            )
            try:
                parsed = json.loads(result.stdout)
                public_ip = parsed.get("value", parsed)
            except Exception:
                public_ip = result.stdout.strip().strip('"')
            print(f"\n[✓] Application deployed and available at: http://{public_ip}/")
        except subprocess.CalledProcessError as e:
            print(f"❌ A Terraform command failed. Details:\n{e.stderr or e.stdout or ''}")
            print("💡 Ensure you have the required CLI and are authenticated to GCP.")
            sys.exit(1)

    # Azure path
    elif cloud_provider and re.search(r'\b(azure|microsoft\s+azure)\b', cloud_provider, flags=re.I):
        # Ensure Azure CLI and login
        if shutil.which("az") is None:
            print("❌ Azure CLI (az) not found. Install it and run `az login`.")
            sys.exit(1)
        chk = subprocess.run(["az", "account", "show"], check=False, capture_output=True, text=True)
        if chk.returncode != 0:
            print("❌ Not logged into Azure. Run `az login` or configure a service principal.")
            sys.exit(1)

        # Write TF (Azure) and apply
        try:
            write_terraform_files("Azure", app_port, repo_name, output_dir)
        except RuntimeError as e:
            print(f"❌ {e}")
            sys.exit(1)

        print("\n--- Executing Terraform commands (Azure) ---")
        try:
            subprocess.run(["terraform", "init", "-reconfigure"], cwd=output_dir, check=True)
            print("✅ Terraform init successful.")
            subprocess.run(["terraform", "apply", "-auto-approve", "-input=false"], cwd=output_dir, check=True)
            print("✅ Terraform apply successful.")

            result = subprocess.run(
                ["terraform", "output", "-json", "public_ip"],
                cwd=output_dir, check=True, capture_output=True, text=True
            )
            try:
                parsed = json.loads(result.stdout)
                public_ip = parsed.get("value", parsed)
            except Exception:
                public_ip = result.stdout.strip().strip('"')
            print(f"\n[✓] Application deployed and available at: http://{public_ip}/")
        except subprocess.CalledProcessError as e:
            print(f"❌ A Terraform command failed. Details:\n{e.stderr or e.stdout or ''}")
            print("💡 Ensure Terraform and Azure credentials are set correctly (subscription, SSH key, etc.).")
            sys.exit(1)

    # AWS path (LLM determines instance type; AZ chosen to support it in TF writer)
    elif cloud_provider and re.search(r'\b(aws|amazon\s+web\s+services|amazon)\b', cloud_provider, flags=re.I):
        # Ensure AWS CLI and credentials
        if shutil.which("aws") is None:
            print("❌ AWS CLI not found. Install AWS CLI v2 and configure credentials.")
            sys.exit(1)
        if shutil.which("terraform") is None:
            print("❌ terraform not found. Install Terraform and try again.")
            sys.exit(1)

        which_aws = shutil.which("aws")
        ver = subprocess.run([which_aws, "--version"], check=False, capture_output=True, text=True)
        print(f"ℹ️ Using AWS CLI at: {which_aws} -> {(ver.stdout or ver.stderr).strip()}")

        # Verify credentials
        who = subprocess.run(["aws", "sts", "get-caller-identity"], check=False, capture_output=True, text=True)
        if who.returncode != 0:
            print("❌ AWS credentials not configured. Use `aws configure sso` (v2) or `aws configure`.")
            print(who.stderr or who.stdout or "")
            sys.exit(1)

        # LLM picks instance type unless user already set AWS_INSTANCE_TYPE
        region = os.getenv("AWS_REGION", "us-east-1")
        inst_type = os.getenv("AWS_INSTANCE_TYPE")
        if not inst_type:
            inst_type = choose_aws_instance_type(app_type, region)
            print(f"🤖 LLM-selected AWS instance type for region {region}: {inst_type}")
            os.environ["AWS_INSTANCE_TYPE"] = inst_type  # used by the TF writer

        # Write TF (AWS)
        try:
            write_terraform_files_aws(app_port, repo_name, output_dir)
        except RuntimeError as e:
            print(f"❌ {e}")
            sys.exit(1)

        # Clean any stale TF state from previous cloud runs in this folder
        tf_dir = output_dir / ".terraform"
        tf_lock = output_dir / ".terraform.lock.hcl"
        if tf_dir.exists():
            print("🧹 Removing previous .terraform directory to avoid stale provider locks...")
            shutil.rmtree(tf_dir, ignore_errors=True)
        if tf_lock.exists():
            print("🧹 Removing previous .terraform.lock.hcl...")
            try:
                tf_lock.unlink()
            except Exception:
                pass

        # Purge leftover Azure/GCP .tf files in this folder (defense-in-depth)
        for p in output_dir.glob("*.tf"):
            try:
                txt = p.read_text()
            except Exception:
                continue
            if ('provider "azurerm"' in txt) or ("azurerm_" in txt) or ('provider "google"' in txt) or ("google_compute_" in txt):
                print(f"🧹 Removing leftover non-AWS file: {p.name}")
                try:
                    p.unlink()
                except Exception:
                    pass

        # Backup/remove state if it references non-AWS resources
        state_path = output_dir / "terraform.tfstate"
        if state_path.exists():
            try:
                data = json.loads(state_path.read_text())
                non_aws = []
                for res in (data.get("resources") or []):
                    t = res.get("type", "")
                    if not t.startswith("aws_"):
                        non_aws.append(t)
                if non_aws:
                    backup = output_dir / "terraform.tfstate.azure_or_gcp.backup"
                    print(f"🧳 Found non-AWS resources in state {set(non_aws)}; backing up -> {backup.name}")
                    state_path.replace(backup)
            except Exception:
                backup = output_dir / "terraform.tfstate.backup"
                print(f"🧳 State unreadable; backing up -> {backup.name}")
                try:
                    state_path.replace(backup)
                except Exception:
                    pass

        # Use a persistent plugin cache for reliable installs
        tf_env = os.environ.copy()
        tf_env.setdefault("TF_PLUGIN_CACHE_DIR", os.path.expanduser("~/.terraform.d/plugin-cache"))
        os.makedirs(tf_env["TF_PLUGIN_CACHE_DIR"], exist_ok=True)

        print("\n--- Executing Terraform commands (AWS) ---")
        try:
            print("$ terraform init -reconfigure -upgrade")
            subprocess.run(["terraform", "init", "-reconfigure", "-upgrade"], cwd=output_dir, check=True, env=tf_env)
            print("✅ Terraform init successful.")

            print("$ terraform apply -auto-approve -input=false")
            subprocess.run(["terraform", "apply", "-auto-approve", "-input=false"], cwd=output_dir, check=True, env=tf_env)
            print("✅ Terraform apply successful.")

            result = subprocess.run(
                ["terraform", "output", "-json", "public_ip"],
                cwd=output_dir, check=True, capture_output=True, text=True, env=tf_env
            )
            try:
                parsed = json.loads(result.stdout)
                public_ip = parsed.get("value", parsed)
            except Exception:
                public_ip = result.stdout.strip().strip('"')

            print(f"\n[✓] Application deployed and available at: http://{public_ip}/")
        except subprocess.CalledProcessError as e:
            print(f"❌ A Terraform command failed. Details:\n{e.stderr or e.stdout or ''}")
            print("💡 If it hung on provider install, macOS may have blocked the binary (Privacy & Security → Allow).")
            print("💡 You can also retry with: TF_LOG=DEBUG terraform init -reconfigure -upgrade")
            sys.exit(1)

    else:
        print(f"❌ Deployment for {cloud_provider} not yet supported.")







if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(130)
    except Exception as e:
        print(f"❌ An unexpected error occurred: {e}")
        sys.exit(1)
