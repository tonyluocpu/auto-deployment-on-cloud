#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, json, re, requests, textwrap, subprocess, tempfile, getpass, uuid
from pathlib import Path
from urllib.parse import urlparse

# ===================== CHAT BACKEND =====================

def chat_complete(messages, provider="openrouter", model=None, api_key=None):
    """
    Minimal chat wrapper. Supports:
      provider="openrouter" -> https://openrouter.ai/api/v1/chat/completions
      provider="openai"     -> https://api.openai.com/v1/chat/completions
    """
    if provider not in ("openrouter", "openai"):
        raise ValueError("provider must be 'openrouter' or 'openai'")

    model = model or ("openai/gpt-4o-mini" if provider == "openrouter" else "gpt-4o-mini")
    url = "https://openrouter.ai/api/v1/chat/completions" if provider == "openrouter" else "https://api.openai.com/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if provider == "openrouter":
        headers["HTTP-Referer"] = "http://localhost"
        headers["X-Title"] = "AutoDeployment Chat"

    payload = {"model": model, "messages": messages, "temperature": 0}
    r = requests.post(url, headers=headers, json=payload, timeout=180)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]

# ===================== GITHUB HELPERS =====================

GITHUB_API = "https://api.github.com"
GITHUB_HEADERS = {"Accept": "application/vnd.github+json"}

# LLM I/O safety budgets (tune as needed)
MAX_FILES_TO_FETCH = 16
MAX_BYTES_PER_FILE = 200_000          # hard cap per file (~200 KB)
MAX_TOTAL_BYTES_FOR_LLM = 200_000     # total bytes across snippets sent to LLM

def parse_github_url(url: str):
    if url.startswith("git@github.com:"):
        path = url.split("git@github.com:")[-1]
        if path.endswith(".git"): path = path[:-4]
        owner, repo = path.strip("/").split("/", 1)
        return owner, repo, None

    u = urlparse(url)
    if u.netloc != "github.com":
        raise ValueError("Not a GitHub URL")
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("GitHub URL should look like https://github.com/<owner>/<repo>")
    owner, repo = parts[0], parts[1].replace(".git", "")
    ref = None
    if len(parts) >= 4 and parts[2] == "tree":
        ref = parts[3]
    return owner, repo, ref

def resolve_default_branch(owner, repo, gh_token=None):
    headers = dict(GITHUB_HEADERS)
    if gh_token: headers["Authorization"] = f"Bearer {gh_token}"
    r = requests.get(f"{GITHUB_API}/repos/{owner}/{repo}", headers=headers)
    if r.status_code == 404:
        raise FileNotFoundError(f"GitHub repo not found or private: {owner}/{repo}. If private, provide a GitHub token.")
    r.raise_for_status()
    return r.json().get("default_branch", "main")

def get_tree(owner, repo, ref=None, gh_token=None):
    headers = dict(GITHUB_HEADERS)
    if gh_token: headers["Authorization"] = f"Bearer {gh_token}"
    if ref is None:
        ref = resolve_default_branch(owner, repo, gh_token=gh_token)

    r = requests.get(f"{GITHUB_API}/repos/{owner}/{repo}/git/refs/heads/{ref}", headers=headers)
    if r.status_code == 404:
        # allow tag/sha fallback
        r2 = requests.get(f"{GITHUB_API}/repos/{owner}/{repo}/git/refs/tags/{ref}", headers=headers)
        if r2.status_code == 404:
            sha = ref
        else:
            r2.raise_for_status()
            sha = r2.json()["object"]["sha"]
    else:
        r.raise_for_status()
        sha = r.json()["object"]["sha"]

    r = requests.get(f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{sha}?recursive=1", headers=headers)
    r.raise_for_status()
    data = r.json()
    return sha, data.get("tree", [])

def fetch_raw(owner, repo, ref, path):
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    r = requests.get(url, headers={"Accept": "text/plain"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    content = r.content
    if len(content) > MAX_BYTES_PER_FILE:
        return content[:MAX_BYTES_PER_FILE] + b"\n\n# [TRUNCATED]\n"
    return content

# ===================== LLM-AIDED REPO ANALYSIS =====================

def llm_choose_critical_paths(repo_url:str, all_paths:list[str], provider, model, api_key) -> list[str]:
    """
    Let the model pick the most relevant files for env/runtime/deploy.
    """
    # Keep the list reasonably sized; longer lists are fine, but no need to send hundreds of lines.
    paths_text = "\n".join(all_paths[:5000])
    prompt = f"""
From the file path list below, choose up to {MAX_FILES_TO_FETCH} that MOST LIKELY contain environment/runtime/deployment details:
- README / docs setup sections
- requirements.txt / pyproject.toml / Pipfile / package.json
- Dockerfile / docker-compose / compose.yml
- Procfile / runtime.txt / Makefile / start scripts
- .env / .env.example / config files
- Terraform (*.tf), Kubernetes manifests (*.yml/*.yaml), Cloud Build/Actions workflows

RULES:
- Only return paths that EXIST in the list.
- Prefer root-level files when duplicates exist.
- Output STRICT JSON: an array of strings (no prose).

Repo: {repo_url}

Paths:
{paths_text}
"""
    raw = chat_complete(
        [
            {"role": "system", "content": "You are a meticulous DevOps analyst. Output valid JSON only."},
            {"role": "user", "content": prompt.strip()},
        ],
        provider=provider, model=model, api_key=api_key
    )
    m = re.search(r"\[.*\]", raw, flags=re.S)
    if not m:
        # fall back to first N root files if model fails
        roots = [p for p in all_paths if "/" not in p][:MAX_FILES_TO_FETCH]
        return roots
    try:
        arr = json.loads(m.group(0))
        return [p for p in arr if isinstance(p,str) and p in all_paths][:MAX_FILES_TO_FETCH]
    except Exception:
        roots = [p for p in all_paths if "/" not in p][:MAX_FILES_TO_FETCH]
        return roots

def build_snippets_full(files_payload, total_budget=MAX_TOTAL_BYTES_FOR_LLM):
    """
    Build Markdown snippets with as-close-to-full contents as possible, honoring a total byte budget.
    """
    used = 0
    blocks = []
    for path, text in files_payload:
        blob = text if isinstance(text, str) else str(text)
        # ensure we don't blow the total budget
        b = blob.encode("utf-8", errors="ignore")
        remaining = max(0, total_budget - used)
        if remaining <= 0:
            break
        if len(b) > remaining:
            # trim to remaining bytes boundary
            blob = b[:remaining].decode("utf-8", errors="ignore") + "\n[TRUNCATED]\n"
        used += len(blob.encode("utf-8", errors="ignore"))
        # Protect braces slightly to avoid confusing some models
        safe_blob = blob.replace("{", "﹛").replace("}", "﹜")
        blocks.append(f"### {path}\n```\n{safe_blob}\n```")
    return "\n".join(blocks)

def llm_infer_env_schema(files_payload, provider, model, api_key):
    """
    Read the selected files' contents CAREFULLY and infer env vars.
    Return list[{name, required, default, description}]
    """
    snippets = build_snippets_full(files_payload)
    prompt = f"""
You are deriving runtime environment variables for deployment.

READ THE PROVIDED FILE CONTENTS CAREFULLY (word-for-word). Treat them as authoritative.
Extract ONLY variables that are explicitly referenced (e.g., .env/.env.example, README sections,
Docker/compose env, Python os.getenv/os.environ, JS process.env, config files, YAML, Makefiles).

Return STRICT JSON: an array of objects with fields:
- name (string, UPPER_SNAKE_CASE, exactly as referenced)
- required (boolean)
- default (string or null)  # null if no default in the files
- description (string, brief; may be empty)

Do NOT invent variables. If uncertain, omit it.

Files:
{snippets}

Respond ONLY with a JSON array.
"""
    raw = chat_complete(
        [
            {"role": "system", "content": "You are a precise DevOps analyst. Output valid JSON only."},
            {"role": "user", "content": prompt.strip()},
        ],
        provider=provider, model=model, api_key=api_key
    )
    m = re.search(r"\[.*\]", raw, flags=re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
        out = []
        for it in arr:
            if not isinstance(it, dict): 
                continue
            name = it.get("name")
            if not isinstance(name, str) or not name:
                continue
            out.append({
                "name": name.upper(),
                "required": bool(it.get("required", False)),
                "default": it.get("default") if isinstance(it.get("default"), (str, type(None))) else None,
                "description": (it.get("description") or "")
            })
        return out[:20]
    except Exception:
        return []

_ENV_PATTERNS = [
    r"os\.environ\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]",
    r"os\.getenv\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]",
    r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)",
    r"\b([A-Z][A-Z0-9_]{2,})\s*=",         # VAR=...
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",     # ${VAR}
]

def fallback_extract_env_keys(files_payload):
    """Regex fallback if the model returns nothing."""
    found = set()
    for _, text in files_payload:
        t = text if isinstance(text, str) else str(text)
        for pat in _ENV_PATTERNS:
            for m in re.finditer(pat, t):
                var = m.group(1).upper()
                if var.lower() in {"path", "home", "user", "pwd"}:
                    continue
                if len(var) < 3:
                    continue
                found.add(var)
    return sorted(found)[:20]

def summarize_with_llm(repo_url, files_payload, provider, model, api_key):
    """
    Read selected files carefully and produce STRICT JSON spec:
    provider, project_id, region, zone, language, port, start_command, env (object).
    """
    snippets = build_snippets_full(files_payload)
    prompt = f"""
You are planning a deployment for the given repository.

READ THE PROVIDED FILE CONTENTS CAREFULLY (word-for-word). Treat them as authoritative.
From these files, infer:
- language/runtime (python, node, etc.)
- port to expose (or a reasonable default)
- start_command that binds to 0.0.0.0:<port>
- env object: include only variables actually referenced in the files; use defaults if stated, otherwise "".

Return STRICT JSON **object** with keys:
- provider: "gcp"
- project_id: null if unknown (the user will fill this)
- region: default "us-central1" if unsure
- zone: default "us-central1-a" if unsure
- language: string
- port: integer
- start_command: string (must bind 0.0.0.0:<port>)
- env: object of key->string ("" if unknown, or default if present)

Files:
{snippets}

Respond ONLY with a JSON object (no prose).
"""
    raw = chat_complete(
        [
            {"role": "system", "content": "You are a senior DevOps engineer. Output strict JSON only."},
            {"role": "user", "content": prompt.strip()},
        ],
        provider=provider, model=model, api_key=api_key
    )
    m = re.search(r"\{.*\}", raw, flags=re.S)
    if not m:
        raise ValueError("Model did not return JSON for deployment spec:\n" + raw)
    return json.loads(m.group(0))

def inspect_repo_and_plan(repo_url, chat_provider, chat_model, chat_key, gh_token=None):
    owner, repo, ref = parse_github_url(repo_url)
    if ref is None:
        ref = resolve_default_branch(owner, repo, gh_token=gh_token)

    _, tree = get_tree(owner, repo, ref, gh_token=gh_token)
    all_paths = [t["path"] for t in tree if t.get("type") == "blob"]

    critical_paths = llm_choose_critical_paths(repo_url, all_paths, chat_provider, chat_model, chat_key)

    # fetch contents for chosen files
    files_payload = []
    for path in critical_paths:
        raw = fetch_raw(owner, repo, ref, path)
        if not raw:
            continue
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = "[binary content omitted]"
        files_payload.append((path, text))

    # Human summary (for print)
    if critical_paths:
        summary_line = "I read the repo and the following files likely contained critical environment/deployment info:\n- " + "\n- ".join(critical_paths)
    else:
        summary_line = "I read the repo, but did not find obvious environment/deployment files. Proceeding with defaults."

    # Produce spec (includes env object) using careful-read prompt
    spec = summarize_with_llm(repo_url, files_payload, chat_provider, chat_model, chat_key)

    # Safety defaults
    spec.setdefault("provider", "gcp")
    spec.setdefault("region", "us-central1")
    spec.setdefault("zone", "us-central1-a")
    spec.setdefault("env", {})

    # If env is missing or empty, try regex fallback to seed keys
    if not isinstance(spec.get("env"), dict) or not spec["env"]:
        candidates = fallback_extract_env_keys(files_payload)
        spec["env"] = {k: "" for k in candidates}

    return summary_line, files_payload, spec

# ===================== TERRAFORM (GCP VM) =====================

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

resource "google_project_service" "compute" {
  project            = var.project_id
  service            = "compute.googleapis.com"
  disable_on_destroy = false
}

resource "google_compute_firewall" "fw" {
  name    = "${var.name_prefix}-fw"
  network = "default"

  allow { protocol = "tcp" ports = [tostring(var.app_port)] }
  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["${var.name_prefix}"]
}

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
    access_config {}
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

STARTUP = r"""#!/usr/bin/env bash
set -euo pipefail
echo "[startup] begin" | tee /var/log/autodeploy.log
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git curl ufw

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

# Export env vars
cat <<'EOF' >/etc/profile.d/app_env.sh
${env_lines}
EOF
chmod +x /etc/profile.d/app_env.sh
. /etc/profile.d/app_env.sh

# Replace localhost bindings (best-effort)
grep -rl "127.0.0.1" . | xargs -I{} sed -i "s/127.0.0.1/0.0.0.0/g" {} || true
grep -rl "localhost" .  | xargs -I{} sed -i "s/localhost/0.0.0.0/g" {} || true

ufw allow ${app_port} || true
ufw --force enable || true

nohup bash -lc "${start_command}" >> /var/log/app_start.log 2>&1 &
echo "[startup] done" | tee -a /var/log/autodeploy.log
"""

def sh(cmd, cwd=None):
    print(f"$ {' '.join(cmd)}")
    p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if p.returncode != 0:
        print(p.stdout); print(p.stderr)
        raise RuntimeError(f"Command failed: {' '.join(cmd)}")
    return p.stdout.strip()

def write_tf_project(workdir: Path, spec: dict):
    (workdir / "main.tf").write_text(TF_MAIN)
    (workdir / "variables.tf").write_text(TF_VARS)
    (workdir / "startup.tftpl").write_text(STARTUP)

    env_lines = [f'export {k}="{str(v)}"' for k, v in (spec.get("env") or {}).items()]
    name_prefix = spec.get("name_prefix") or f"autodeploy-{uuid.uuid4().hex[:6]}"

    tfvars = {
        "name_prefix":   name_prefix,
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
    with tempfile.TemporaryDirectory(prefix="autodeploy-gcp-") as td:
        wd = Path(td)
        write_tf_project(wd, spec)
        print("[*] Terraform apply on GCP...")
        sh(["terraform", "init", "-input=false"], cwd=wd)
        sh(["terraform", "apply", "-auto-approve"], cwd=wd)
        out = sh(["terraform", "output", "-json"], cwd=wd)
        outputs = json.loads(out)
        ip = outputs["external_ip"]["value"]
        name_prefix = outputs.get("name_prefix", {}).get("value")
        url = f"http://{ip}:{spec['port']}"
        print("\n=== Deployment Complete (GCP) ===")
        print("External IP:", ip)
        print("Name Prefix:", name_prefix or "(unknown)")
        print("App URL:    ", url)
        print("Logs: /var/log/autodeploy.log and /var/log/app_start.log (ssh if needed)")
        return {"external_ip": ip, "url": url, "name_prefix": name_prefix}

# ===================== INTERACTIVE FLOW =====================

def prompt_hidden(label):
    try:
        return getpass.getpass(label)
    except Exception:
        return input(label)

def ensure_adc_env():
    """Auto-wire GOOGLE_APPLICATION_CREDENTIALS to default ADC path if unset."""
    if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        return
    default_path = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
    if os.path.isfile(default_path):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = default_path

def main():
    print("=== AutoDeployment Chat (GCP) ===")
    ensure_adc_env()

    # Chat provider
    provider = (os.getenv("CHAT_PROVIDER") or "openrouter").strip().lower()
    if provider not in ("openrouter", "openai"):
        provider = "openrouter"
    default_model = "openai/gpt-4o-mini" if provider == "openrouter" else "gpt-4o-mini"
    model = os.getenv("CHAT_MODEL") or default_model

    api_key_env = "OPENROUTER_API_KEY" if provider == "openrouter" else "OPENAI_API_KEY"
    api_key = os.getenv(api_key_env) or prompt_hidden(f"Paste your {provider} API key (hidden): ")
    if not api_key:
        print("No API key provided. Exiting."); sys.exit(1)

    # Optional GitHub token
    gh_token = os.getenv("GITHUB_TOKEN")
    if not gh_token:
        ans = input("Do you have a GitHub token to avoid rate limits or access private repos? [y/N]: ").strip().lower()
        if ans == "y":
            gh_token = prompt_hidden("Paste GitHub token (hidden): ")

    # Repo URL
    repo_url = input("GitHub repo URL to deploy: ").strip()
    if not repo_url:
        print("A GitHub repo URL is required. Exiting."); sys.exit(1)

    # Inspect & plan (LLM careful-read)
    print("\n[*] Inspecting repository & generating deployment plan...")
    try:
        summary_line, files_payload, spec = inspect_repo_and_plan(
            repo_url, chat_provider=provider, chat_model=model, chat_key=api_key, gh_token=gh_token
        )
    except Exception as e:
        print("Failed to inspect repo or get plan:", e); sys.exit(1)

    # Show summary
    print("\n=== Repo Analysis Summary ===")
    print(summary_line)

    # Ensure project id
    if not spec.get("project_id") or spec["project_id"] in (None, "", "MISSING"):
        project_id = input("\nEnter your GCP project_id (required): ").strip()
        if not project_id:
            print("project_id is required. Exiting."); sys.exit(1)
        spec["project_id"] = project_id

    # Sensible defaults
    spec.setdefault("provider", "gcp")
    spec.setdefault("region", "us-central1")
    spec.setdefault("zone", "us-central1-a")
    spec.setdefault("language", "python")
    spec.setdefault("port", 8000)
    spec.setdefault("start_command", "python3 app.py")
    spec.setdefault("env", {})

    # Print inferred env (informational only)
    if spec.get("env"):
        print("\n=== Inferred Environment Variables (auto) ===")
        for k, v in spec["env"].items():
            shown = v if isinstance(v, str) and v != "" else "(empty)"
            print(f"- {k}: {shown}")

    # Review/override
    print("\n=== Proposed Deployment Spec (editable) ===")
    print(json.dumps(spec, indent=2))
    edit = input("Edit any fields? (type JSON to override, or press Enter to accept): ").strip()
    if edit:
        try:
            user_spec = json.loads(edit)
            spec.update(user_spec)
        except Exception as e:
            print("Invalid JSON override, ignoring. Error:", e)

    # Credentials hint (if ADC not set)
    if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        print("\nGCP credentials not detected in GOOGLE_APPLICATION_CREDENTIALS.")
        print("Terraform will try ADC at ~/.config/gcloud/application_default_credentials.json.")
        print("If it fails, run: gcloud auth application-default login")

    # Deploy
    go = input("\nProceed to deploy on GCP? [Y/n]: ").strip().lower() or "y"
    if go != "y":
        print("Cancelled."); sys.exit(0)

    try:
        result = deploy_on_gcp({
            "project_id":   spec["project_id"],
            "region":       spec["region"],
            "zone":         spec["zone"],
            "repo_url":     repo_url,
            "port":         int(spec["port"]),
            "start_command":spec["start_command"],
            "env":          spec.get("env") or {},
            "language":     spec.get("language","python"),
            "machine_type": spec.get("machine_type","e2-small"),
        })
        print("\n=== Final Result ===")
        print(json.dumps(result, indent=2))
        print("\nGive the instance ~1–2 minutes to finish first-time package installs.")
    except Exception as e:
        print("Deployment failed:", e); sys.exit(1)

if __name__ == "__main__":
    main()
