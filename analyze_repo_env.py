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
"""

import os, sys, re, json, requests, textwrap
from urllib.parse import urlparse
from pathlib import Path

# ---------------- Chat backend ----------------

def chat_complete(messages, provider="openrouter", model=None, api_key=None, timeout=240):
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
        headers["X-Title"] = "Repo Environment Analyzer"

    payload = {"model": model, "messages": messages, "temperature": 0}
    r = requests.post(url, headers=headers, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]

# ---------------- GitHub helpers ----------------

GITHUB_API = "https://api.github.com"
GITHUB_HEADERS = {"Accept": "application/vnd.github+json"}
MAX_FILES_TO_FETCH = 20
MAX_BYTES_PER_FILE = 250_000
MAX_TOTAL_BYTES_FOR_LLM = 350_000  # overall budget for snippets sent to LLM

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
    ref = None
    if len(parts) >= 4 and parts[2] == "tree":
        ref = parts[3]
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
            sha = ref  # assume direct sha
        else:
            r2.raise_for_status()
            sha = r2.json()["object"]["sha"]
    else:
        r.raise_for_status()
        sha = r.json()["object"]["sha"]

    r = requests.get(f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{sha}?recursive=1", headers=headers)
    r.raise_for_status()
    data = r.json()
    return ref, data.get("tree", [])

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

# ---------------- LLM-aided repo analysis ----------------

def llm_choose_critical_paths(repo_url: str, all_paths: list[str], provider, model, api_key) -> list[str]:
    # Keep list length reasonable for the prompt
    paths_text = "\n".join(all_paths[:6000])
    prompt = f"""
From the file paths below, choose up to {MAX_FILES_TO_FETCH} that MOST LIKELY contain environment/runtime/dependency details:
- README / docs setup
- requirements.txt / pyproject.toml / Pipfile / poetry.lock
- package.json / yarn.lock / pnpm-lock.yaml
- Dockerfile / docker-compose*.yml / compose.yml
- Procfile / runtime.txt / Makefile / start scripts
- .python-version / .tool-versions / .nvmrc / .node-version
- .env / .env.example / env.example
- setup.cfg / setup.py / pyproject config
- conda.yml / environment.yml
- any config that pins versions

Rules:
- If a README file exists (any case), ALWAYS include it first.
- Only return paths that EXIST in the list.
- Prefer root-level files when duplicates exist.
- Output STRICT JSON: an array of strings (no prose).

Repo: {repo_url}

Paths:
{paths_text}
"""
    raw = chat_complete(
        [
            {"role": "system", "content": "You are a meticulous environment auditor. Output valid JSON only."},
            {"role": "user", "content": prompt.strip()},
        ],
        provider=provider, model=model, api_key=api_key
    )
    m = re.search(r"\[.*\]", raw, flags=re.S)
    if not m:
        # crude fallback: typical root files
        roots = [p for p in all_paths if "/" not in p]
        preferred = [x for x in roots if x.lower() in {
            "readme.md","readme","readme.txt","readme.rst","readme.mdown","readme.markdown",
            "requirements.txt","pyproject.toml","pipfile","poetry.lock",
            "package.json","yarn.lock","pnpm-lock.yaml",
            "dockerfile","docker-compose.yml","compose.yml",
            ".python-version",".tool-versions",".nvmrc",".node-version",
            ".env",".env.example","environment.yml","conda.yml","runtime.txt","procfile","makefile"}]
        return (preferred + roots)[:MAX_FILES_TO_FETCH]
    try:
        arr = json.loads(m.group(0))
        picked = [p for p in arr if isinstance(p, str) and p in all_paths]
        if not picked:
            raise ValueError("empty selection")
        return picked[:MAX_FILES_TO_FETCH]
    except Exception:
        roots = [p for p in all_paths if "/" not in p]
        return roots[:MAX_FILES_TO_FETCH]

def build_snippets_full(files_payload, total_budget=MAX_TOTAL_BYTES_FOR_LLM):
    used = 0
    blocks = []
    for path, text in files_payload:
        blob = text if isinstance(text, str) else str(text)
        b = blob.encode("utf-8", errors="ignore")
        remain = max(0, total_budget - used)
        if remain <= 0:
            break
        if len(b) > remain:
            blob = b[:remain].decode("utf-8", errors="ignore") + "\n[TRUNCATED]\n"
        used += len(blob.encode("utf-8", errors="ignore"))
        safe_blob = blob.replace("{", "﹛").replace("}", "﹜")
        blocks.append(f"### {path}\n```\n{safe_blob}\n```")
    return "\n".join(blocks)

def llm_environment_report(repo_url, files_payload, provider, model, api_key) -> dict:
    """
    Ask the model to produce a detailed environment report.
    """
    snippets = build_snippets_full(files_payload)
    prompt = f"""
READ THESE FILES CAREFULLY (word-for-word). Treat them as authoritative.

Output a STRICT JSON object with:
- repo_url: string
- language: string (python, node, go, java, etc.)
- language_version: string (best value you can derive). If an explicit version exists (e.g., ".python-version", "runtime.txt", ".tool-versions", "engines" field, Dockerfile FROM tag), return that.
  If not explicit, infer a BEST-ESTIMATE version or range based on dependencies/frameworks and typical support (e.g., ">=3.11", "~16", ">=1.20"). Do not leave null unless truly unknowable.
- frameworks: array of strings (e.g., ["flask","fastapi"])
- package_manager: string or null (pip/poetry/pipenv/conda/npm/yarn/pnpm)
- dependencies: array of objects {{ "name": string, "version": string|null, "source": "requirements|pyproject|package.json|lock|env|other" }}
- dev_dependencies: array of objects (same shape), may be empty
- system_packages: array of strings (apt packages etc.), may be empty
- env_vars: array of objects {{ "name": string, "required": boolean, "default": string|null, "description": string }}
- start_commands: array of strings (all plausible commands found)
- ports: array of integers (guess from files if needed)
- notes: array of short strings for important caveats (e.g., GPU/CUDA hints, ABI constraints)

Rules:
- Include ONLY dependencies actually present in the provided files; pull versions from lockfiles when available.
- If a version is a range, keep it (e.g., ">=1.2,<2.0"); if missing, use null.
- Do NOT invent secrets/env vars; only include those explicitly referenced (.env files, README, os.getenv/process.env).
- Keep JSON compact and precise. No prose outside JSON.

Files:
{snippets}

Respond ONLY with the JSON object.
"""
    raw = chat_complete(
        [
            {"role": "system", "content": "You are a senior build & environment analyst. Output STRICT JSON only."},
            {"role": "user", "content": prompt.strip()},
        ],
        provider=provider, model=model, api_key=api_key
    )
    m = re.search(r"\{.*\}", raw, flags=re.S)
    if not m:
        raise ValueError("Model did not return JSON.")
    return json.loads(m.group(0))

# ---------------- Runner ----------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python analyze_repo_env.py <github_repo_url>")
        sys.exit(1)
    repo_url = sys.argv[1].strip()

    provider = (os.getenv("CHAT_PROVIDER") or "openrouter").strip().lower()
    if provider not in ("openrouter", "openai"):
        provider = "openrouter"
    model = os.getenv("CHAT_MODEL") or ("openai/gpt-4o-mini" if provider == "openrouter" else "gpt-4o-mini")
    api_key_env = "OPENROUTER_API_KEY" if provider == "openrouter" else "OPENAI_API_KEY"
    api_key = os.getenv(api_key_env)
    if not api_key:
        print(f"Missing {api_key_env}. Set it and retry."); sys.exit(1)

    gh_token = os.getenv("GITHUB_TOKEN")
    if gh_token:
        print("[*] Using GitHub token for API access.")

    # Fetch tree, let LLM pick files, download them
    print("[*] Enumerating repository...")
    owner, repo, ref = parse_github_url(repo_url)
    if ref is None:
        ref, _ = resolve_default_branch(owner, repo, gh_token=gh_token), None
    ref, tree = get_tree(owner, repo, ref, gh_token=gh_token)
    all_paths = [t["path"] for t in tree if t.get("type") == "blob"]
    if not all_paths:
        print("No files found in repo tree."); sys.exit(1)

    # Patch 1: detect README-like files
    likely_readmes = [p for p in all_paths if p.lower() in (
        "readme.md","readme","readme.txt","readme.rst","readme.mdown","readme.markdown"
    )]
    if likely_readmes:
        print("[*] README detected at:", ", ".join(likely_readmes))

    print("[*] Selecting critical files via LLM...")
    critical_paths = llm_choose_critical_paths(repo_url, all_paths, provider, model, api_key)

    # Patch 2: Always include README (if present) ahead of LLM picks
    # ensure uniqueness order-preserving, then trim to budget
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
    for p, _ in files_payload:
        print("-", p)

    print("\n[*] Generating environment report with LLM...")
    try:
        report = llm_environment_report(repo_url, files_payload, provider, model, api_key)
    except Exception as e:
        print("Failed to generate environment report:", e)
        sys.exit(1)

    # Save JSON
    Path("env_report.json").write_text(json.dumps(report, indent=2))
    print('\nSaved full JSON to: env_report.json')

    # Pretty print essentials
    def _fmt_deps(items):
        out = []
        for d in items or []:
            name = d.get("name")
            ver = d.get("version")
            src = d.get("source")
            if name:
                if ver: out.append(f"{name}=={ver}  ({src})")
                else:   out.append(f"{name}  ({src})")
        return out

    print("\n=== Environment Summary ===")
    print("Repo:            ", report.get("repo_url") or repo_url)
    print("Language:        ", report.get("language"))
    print("Language version:", report.get("language_version"))  # may be an inferred range like ">=3.11"
    print("Frameworks:      ", ", ".join(report.get("frameworks") or []) or "-")
    print("Package manager: ", report.get("package_manager") or "-")

    deps = _fmt_deps(report.get("dependencies"))
    dev_deps = _fmt_deps(report.get("dev_dependencies"))
    if deps:
        print("\nDependencies:")
        for line in deps: print("  -", line)
    else:
        print("\nDependencies:    -")

    if dev_deps:
        print("\nDev Dependencies:")
        for line in dev_deps: print("  -", line)

    sys_pkgs = report.get("system_packages") or []
    if sys_pkgs:
        print("\nSystem packages:")
        for p in sys_pkgs: print("  -", p)

    envs = report.get("env_vars") or []
    if envs:
        print("\nEnvironment variables:")
        for e in envs:
            name = e.get("name")
            req = "required" if e.get("required") else "optional"
            default = e.get("default")
            desc = e.get("description") or ""
            default_s = default if isinstance(default, str) else "-"
            print(f"  - {name} [{req}] default={default_s} {('- ' + desc) if desc else ''}")
    else:
        print("\nEnvironment variables: -")

    starts = report.get("start_commands") or []
    if starts:
        print("\nStart commands:")
        for s in starts: print("  -", s)

    ports = report.get("ports") or []
    print("\nPorts:           ", ", ".join(str(p) for p in ports) if ports else "-")

    notes = report.get("notes") or []
    if notes:
        print("\nNotes:")
        for n in notes: print("  -", n)

    print("\nDone.")
    # End.
if __name__ == "__main__":
    main()
