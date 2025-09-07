#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
analyze_repo_env.py

Usage:
  OPENROUTER_API_KEY=... python analyze_repo_env.py <github_repo_url>
  # Optional:
  #   export GITHUB_TOKEN=ghp_xxx      # for private repos / higher rate limits
  #   export CHAT_PROVIDER=openai      # default: openrouter
  #   export CHAT_MODEL=gpt-4o-mini    # default for openrouter: openai/gpt-4o-mini
  #   export DOCKERHUB_NAMESPACE=yourhub  # default: cockckd

Outputs:
  - <reponame>_env.json          (repo-specific environment report)
  - env_report.json              (same, generic name)
  - <reponame>.docker            (reference copy of Dockerfile content)
  - <reponame>.env.example       (template .env)
  - <reponame>_local_clone/Dockerfile (build context)
  - Multi-arch image pushed to docker.io/<namespace>/<reponame>:latest
"""

import os, sys, re, json, shlex, requests, shutil, subprocess
from urllib.parse import urlparse
from pathlib import Path

# ---------------- Chat backend ----------------

def chat_complete(messages, provider="openrouter", model=None, api_key=None, timeout=240):
    if provider not in ("openrouter", "openai"):
        raise ValueError("provider must be 'openrouter' or 'openai'")
    model = model or ("openai/gpt-4o-mini" if provider == "openrouter" else "gpt-4o-mini")
    url = "https://openrouter.ai/api/v1/chat/completions" if provider == "openrouter" else "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if provider == "openrouter":
        headers["HTTP-Referer"] = "http://localhost"
        headers["X-Title"] = "Repo Environment Analyzer"
    r = requests.post(url, headers=headers, json={"model": model, "messages": messages, "temperature": 0}, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

# ---------------- GitHub helpers ----------------

GITHUB_API = "https://api.github.com"
GITHUB_HEADERS = {"Accept": "application/vnd.github+json"}
MAX_FILES_TO_FETCH = 20
MAX_BYTES_PER_FILE = 250_000
MAX_TOTAL_BYTES_FOR_LLM = 350_000

def parse_github_url(url: str):
    if url.startswith("git@github.com:"):
        p = url.split("git@github.com:")[-1]
        if p.endswith(".git"): p = p[:-4]
        owner, repo = p.strip("/").split("/", 1)
        return owner, repo, None
    u = urlparse(url)
    if u.netloc != "github.com":
        raise ValueError("Not a GitHub URL")
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("GitHub URL should look like https://github.com/<owner>/<repo>")
    owner, repo = parts[0], parts[1].replace(".git", "")
    ref = parts[3] if len(parts) >= 4 and parts[2] == "tree" else None
    return owner, repo, ref

def resolve_default_branch(owner, repo, gh_token=None):
    headers = dict(GITHUB_HEADERS)
    if gh_token: headers["Authorization"] = f"Bearer {gh_token}"
    r = requests.get(f"{GITHUB_API}/repos/{owner}/{repo}", headers=headers)
    if r.status_code == 404:
        raise FileNotFoundError(f"Repo not found or private: {owner}/{repo}")
    r.raise_for_status()
    return r.json().get("default_branch", "main")

def get_tree(owner, repo, ref=None, gh_token=None):
    headers = dict(GITHUB_HEADERS)
    if gh_token: headers["Authorization"] = f"Bearer {gh_token}"
    if ref is None:
        ref = resolve_default_branch(owner, repo, gh_token=gh_token)
    # Resolve ref -> sha
    r = requests.get(f"{GITHUB_API}/repos/{owner}/{repo}/git/refs/heads/{ref}", headers=headers)
    if r.status_code == 404:
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
    return ref, r.json().get("tree", [])

def fetch_raw(owner, repo, ref, path):
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    r = requests.get(url, headers={"Accept": "text/plain"})
    if r.status_code == 404: return None
    r.raise_for_status()
    content = r.content
    if len(content) > MAX_BYTES_PER_FILE:
        return content[:MAX_BYTES_PER_FILE] + b"\n\n# [TRUNCATED]\n"
    return content

# ---------------- LLM-aided repo analysis ----------------

def llm_choose_critical_paths(repo_url: str, all_paths: list[str], provider, model, api_key) -> list[str]:
    paths_text = "\n".join(all_paths[:6000])
    prompt = f"""
From the file paths below, choose up to {MAX_FILES_TO_FETCH} that MOST LIKELY contain environment/runtime/dependency details:
- README / docs setup
- requirements.txt / pyproject.toml / Pipfile / poetry.lock
- package.json / yarn.lock / pnpm-lock.yaml
- Dockerfile / docker-compose*.yml / compose.yml
- Procfile / runtime.txt / Makefile / start scripts
- version managers (.python-version/.tool-versions/.nvmrc/.node-version)
- .env / .env.example
- setup.cfg / setup.py / conda/environment.yml
- CI that builds/runs the app

Rules:
- If any README exists, include it first.
- Only return paths that EXIST.
- Prefer root-level files when duplicates exist.
- Output STRICT JSON array (no prose).

Repo: {repo_url}

Paths:
{paths_text}
"""
    raw = chat_complete(
        [{"role": "system", "content": "You are a meticulous environment auditor. Output valid JSON only."},
         {"role": "user", "content": prompt.strip()}],
        provider=provider, model=model, api_key=api_key
    )
    m = re.search(r"\[.*\]", raw, flags=re.S)
    if not m:
        roots = [p for p in all_paths if "/" not in p]
        return roots[:MAX_FILES_TO_FETCH]
    try:
        arr = json.loads(m.group(0))
        picked = [p for p in arr if isinstance(p, str) and p in all_paths]
        return picked[:MAX_FILES_TO_FETCH] if picked else [p for p in all_paths if "/" not in p][:MAX_FILES_TO_FETCH]
    except Exception:
        return [p for p in all_paths if "/" not in p][:MAX_FILES_TO_FETCH]

def build_snippets_full(files_payload, total_budget=MAX_TOTAL_BYTES_FOR_LLM):
    used = 0
    blocks = []
    for path, text in files_payload:
        blob = text if isinstance(text, str) else str(text)
        b = blob.encode("utf-8", errors="ignore")
        remain = max(0, total_budget - used)
        if remain <= 0: break
        if len(b) > remain:
            blob = b[:remain].decode("utf-8", errors="ignore") + "\n[TRUNCATED]\n"
        used += len(blob.encode("utf-8", errors="ignore"))
        safe_blob = blob.replace("{", "﹛").replace("}", "﹜")
        blocks.append(f"### {path}\n```\n{safe_blob}\n```")
    return "\n".join(blocks)

def llm_environment_report(repo_url, files_payload, provider, model, api_key) -> dict:
    snippets = build_snippets_full(files_payload)
    prompt = f"""
READ THESE FILES CAREFULLY. Output STRICT JSON only:

Keys:
- repo_url (string)
- language (string)
- language_version (string or best-estimate range like ">=3.11")
- frameworks (string[])
- package_manager (string|null)
- dependencies ({{name, version|null, source}}[])
- dev_dependencies (same shape) []
- system_packages (string[]) []
- env_vars ({{name, required, default|null, description}}[])
- start_commands (string[])
- ports (int[])
- notes (string[])

Files:
{snippets}
"""
    raw = chat_complete(
        [{"role": "system", "content": "You are a meticulous environment auditor. Output STRICT JSON only."},
         {"role": "user", "content": prompt.strip()}],
        provider=provider, model=model, api_key=api_key
    )
    m = re.search(r"\{.*\}", raw, flags=re.S)
    if not m:
        raise ValueError("Model did not return JSON.")
    return json.loads(m.group(0))

# ---------------- Dockerfile rendering ----------------

def _repo_name_from_url(repo_url: str) -> str:
    path = urlparse(repo_url).path.rstrip("/")
    name = path.split("/")[-1]
    return name[:-4] if name.endswith(".git") else name

def _cmd_json_array(cmd: str) -> str:
    return json.dumps(shlex.split(cmd))

def _pick_python_base(language_version: str|None) -> str:
    default = "python:3.11-slim"
    if not isinstance(language_version, str) or not language_version.strip():
        return default
    v = language_version.strip().lower()
    m = re.match(r"^(\d+)\.(\d+)(?:\.\d+)?$", v)
    if m: return f"python:{m.group(1)}.{m.group(2)}-slim"
    if "3.11" in v: return "python:3.11-slim"
    if "3.10" in v: return "python:3.10-slim"
    if "3.9"  in v: return "python:3.9-slim"
    return default

def _pick_node_base(language_version: str|None) -> str:
    default = "node:18-alpine"
    if not isinstance(language_version, str) or not language_version.strip():
        return default
    v = language_version.strip().lower()
    m = re.match(r"^(\d+)(?:\.(\d+))?(?:\.\d+)?$", v)
    if m:
        major = int(m.group(1))
        if major <= 14: return "node:14-alpine"
        if major == 16: return "node:16-alpine"
        if major == 18: return "node:18-alpine"
        if major >= 20: return "node:20-alpine"
    if "20" in v: return "node:20-alpine"
    if "18" in v: return "node:18-alpine"
    if "16" in v: return "node:16-alpine"
    return default

def render_dockerfile(report: dict, files_payload: list[tuple[str, str]]) -> str:
    language = (report.get("language") or "").lower().strip()
    lang_ver = report.get("language_version")
    ports = report.get("ports") or []
    port = int(ports[0]) if ports else 8000
    start_cmds = report.get("start_commands") or []
    start_cmd = start_cmds[0] if start_cmds else ""
    copy_cmds = []
    requirements_path = next((p for p, _ in files_payload if p.endswith("requirements.txt")), None)

    if language == "python":
        base = _pick_python_base(lang_ver)
        if not start_cmd:
            start_cmd = f"gunicorn --bind 0.0.0.0:{port} app:app"
        if requirements_path:
            copy_cmds.append(f"COPY {requirements_path} requirements.txt")
        for path, _ in files_payload:
            if path != requirements_path:
                copy_cmds.append(f"COPY {path} {path}")
        return (f"""# syntax=docker/dockerfile:1
FROM {base}
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1
{''.join(os.linesep + c for c in copy_cmds if 'requirements.txt' in c)}
{('RUN pip install --no-cache-dir -r requirements.txt gunicorn' if requirements_path else '').strip()}
{''.join(os.linesep + c for c in copy_cmds if 'requirements.txt' not in c)}
EXPOSE {port}
CMD {_cmd_json_array(start_cmd)}
""").strip() + "\n"

    if language == "node":
        base = _pick_node_base(lang_ver)
        if not start_cmd:
            start_cmd = "npm start"
        for path, _ in files_payload:
            copy_cmds.append(f"COPY {path} {path}")
        has_pkg = any(p.lower().endswith("package.json") for p, _ in files_payload)
        return (f"""# syntax=docker/dockerfile:1
FROM {base}
WORKDIR /app
ENV NODE_ENV=production
{('COPY package.json package-lock.json* pnpm-lock.yaml* yarn.lock* ./' + os.linesep + 'RUN npm ci || npm install' if has_pkg else '').strip()}
{''.join(os.linesep + c for c in copy_cmds)}
EXPOSE {port}
CMD {_cmd_json_array(start_cmd)}
""").strip() + "\n"

    # generic fallback
    return (f"""# syntax=docker/dockerfile:1
FROM debian:stable-slim
WORKDIR /app
RUN apt-get update -y && apt-get install -y --no-install-recommends \\
      git ca-certificates curl python3 python3-pip \\
    && rm -rf /var/lib/apt/lists/*
{''.join(os.linesep + 'COPY ' + path + ' ./' + path for path, _ in files_payload)}
EXPOSE {port}
CMD {_cmd_json_array(start_cmd or 'python3 app.py')}
""").strip() + "\n"

# ---------------- Runner ----------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python analyze_repo_env.py <github_repo_url>")
        sys.exit(1)
    repo_url = sys.argv[1].strip()

    provider = (os.getenv("CHAT_PROVIDER") or "openrouter").strip().lower()
    if provider not in ("openrouter", "openai"): provider = "openrouter"
    model = os.getenv("CHAT_MODEL") or ("openai/gpt-4o-mini" if provider == "openrouter" else "gpt-4o-mini")
    api_key_env = "OPENROUTER_API_KEY" if provider == "openrouter" else "OPENAI_API_KEY"
    api_key = os.getenv(api_key_env)
    if not api_key:
        print(f"Missing {api_key_env}. Set it and retry."); sys.exit(1)
    gh_token = os.getenv("GITHUB_TOKEN")
    if gh_token: print("[*] Using GitHub token for API access.")

    repo_name = _repo_name_from_url(repo_url)
    namespace = os.getenv("DOCKERHUB_NAMESPACE", "cockckd")
    public_image = f"docker.io/{namespace}/{repo_name}:latest"

    local_repo_path = Path(f"./{repo_name}_local_clone")
    if local_repo_path.exists(): shutil.rmtree(local_repo_path)
    print(f"[*] Cloning repository to {local_repo_path} ...")
    try:
        subprocess.run(["git", "clone", repo_url, str(local_repo_path)], check=True)
    except subprocess.CalledProcessError:
        print("Failed to clone repository. Check URL or GitHub token."); sys.exit(1)

    owner, repo, ref = parse_github_url(repo_url)
    if ref is None:
        ref = resolve_default_branch(owner, repo, gh_token=gh_token)
    ref, tree = get_tree(owner, repo, ref, gh_token=gh_token)
    all_paths = [t["path"] for t in tree if t.get("type") == "blob"]
    if not all_paths:
        print("No files found in repo tree."); sys.exit(1)

    likely_readmes = [p for p in all_paths if p.lower() in ("readme.md","readme","readme.txt","readme.rst","readme.mdown","readme.markdown")]
    critical_paths = llm_choose_critical_paths(repo_url, all_paths, provider, model, api_key)
    critical_paths = list(dict.fromkeys(likely_readmes + critical_paths))[:MAX_FILES_TO_FETCH]

    files_payload = []
    for path in critical_paths:
        raw = fetch_raw(owner, repo, ref, path)
        if not raw: continue
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = "[binary content omitted]"
        files_payload.append((path, text))

    print("\n=== Files Read ===")
    for p, _ in files_payload: print("-", p)

    print("\n[*] Generating environment report with LLM ...")
    try:
        report = llm_environment_report(repo_url, files_payload, provider, model, api_key)
    except Exception as e:
        print("Failed to generate environment report:", e); sys.exit(1)

    # Render Dockerfile + write copies
    docker_txt = render_dockerfile(report, files_payload)
    (local_repo_path / "Dockerfile").write_text(docker_txt)
    Path(f"{repo_name}.docker").write_text(docker_txt)

    # Create .env.example from report
    env_lines = []
    seen = set()
    for ev in (report.get("env_vars") or []):
        name = ev.get("name"); default = ev.get("default"); desc = ev.get("description") or ""
        if not name or name in seen: continue
        seen.add(name)
        line = f"{name}={default}" if (default not in (None, "")) else f"{name}="
        if desc: line += f"  # {desc}"
        env_lines.append(line)
    if "PORT" not in seen: env_lines.append("PORT=8000")
    if "HOST" not in seen: env_lines.append("HOST=0.0.0.0")
    Path(f"{repo_name}.env.example").write_text("\n".join(env_lines) + "\n")

    # Build & push multi-arch image
    print(f"\n[*] Building and pushing multi-arch image → {public_image}")
    try:
        subprocess.run(["docker", "buildx", "create", "--use"], check=False)
        subprocess.run(["docker", "buildx", "inspect", "--bootstrap"], check=False)
        subprocess.run([
            "docker", "buildx", "build",
            "--platform", "linux/amd64,linux/arm64",
            "-t", public_image,
            "--push",
            str(local_repo_path)
        ], check=True)
        print("[✓] Multi-arch image built and pushed.")
    except subprocess.CalledProcessError as e:
        print(f"Docker buildx failed: {e}"); sys.exit(1)

    # Save JSON reports (include image)
    report["public_image"] = public_image
    Path("env_report.json").write_text(json.dumps(report, indent=2))
    Path(f"{repo_name}_env.json").write_text(json.dumps(report, indent=2))

    print("\nDone.")
    print(f"- Report: env_report.json / {repo_name}_env.json")
    print(f"- Image : {public_image}")

if __name__ == "__main__":
    main()
