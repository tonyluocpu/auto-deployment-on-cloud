# auto_repo_probe.py
import os, requests, textwrap, json, re
from urllib.parse import urlparse

# ==== OpenRouter setup ====
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
if not OPENROUTER_API_KEY:
    raise ValueError("Set OPENROUTER_API_KEY env var with your OpenRouter key.")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "HTTP-Referer": "http://localhost",
    "X-Title": "AutoDeployment Inspector"
}
OPENROUTER_MODEL = "openai/gpt-4o-mini"

# ==== GitHub setup ====
GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # optional for private repos / higher rate limits
GITHUB_HEADERS = {"Accept": "application/vnd.github+json"}
if GITHUB_TOKEN:
    GITHUB_HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"

# Safety limits
MAX_FILES_TO_FETCH = 12
MAX_BYTES_PER_FILE = 120_000  # 120 KB
MAX_TREE_LINES_FOR_LLM = 3000  # cap lines sent to model

# --------------------------
# GitHub helpers
# --------------------------
def parse_github_url(url: str):
    u = urlparse(url)
    if u.netloc != "github.com":
        raise ValueError("Not a GitHub URL")
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("GitHub URL must look like https://github.com/<owner>/<repo>")
    owner, repo = parts[0], parts[1].replace(".git","")
    ref = None
    if len(parts) >= 4 and parts[2] == "tree":
        ref = parts[3]
    return owner, repo, ref

def resolve_default_branch(owner, repo):
    r = requests.get(f"{GITHUB_API}/repos/{owner}/{repo}", headers=GITHUB_HEADERS)
    if r.status_code == 404:
        raise FileNotFoundError(f"Repo not found or private: {owner}/{repo}")
    r.raise_for_status()
    return r.json().get("default_branch","main")

def get_tree(owner, repo, ref=None):
    if ref is None:
        ref = resolve_default_branch(owner, repo)
    # resolve SHA for ref/branch
    r = requests.get(f"{GITHUB_API}/repos/{owner}/{repo}/git/refs/heads/{ref}", headers=GITHUB_HEADERS)
    if r.status_code == 404:
        sha = ref  # allow tag/sha
    else:
        r.raise_for_status()
        sha = r.json()["object"]["sha"]
    # full recursive tree
    r = requests.get(f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{sha}?recursive=1", headers=GITHUB_HEADERS)
    r.raise_for_status()
    return sha, r.json().get("tree",[])

def fetch_raw(owner, repo, ref, path):
    url=f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    r=requests.get(url, headers={"Accept":"text/plain"})
    if r.status_code==404: return None
    r.raise_for_status()
    data=r.content
    if len(data)>MAX_BYTES_PER_FILE:
        return data[:MAX_BYTES_PER_FILE]+b"\n\n# [Truncated]\n"
    return data

# --------------------------
# LLM utilities
# --------------------------
def _chat(messages):
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "temperature": 0
    }
    r = requests.post(OPENROUTER_URL, headers=OPENROUTER_HEADERS, json=payload, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

def llm_choose_critical_paths(repo_url:str, all_paths:list[str]) -> list[str]:
    """
    Ask the model to pick up to MAX_FILES_TO_FETCH paths that most likely contain
    environment/deployment/runtime info. We send only the path list (no file contents).
    Returns a Python list of repo-relative paths (as strings).
    """
    # Limit the size going to the model
    paths_text = "\n".join(all_paths[:MAX_TREE_LINES_FOR_LLM])
    prompt = f"""
You are analyzing a repository to prepare deployment.
From the list of file paths below, choose up to {MAX_FILES_TO_FETCH}
that most likely contain critical information about environment, runtime,
dependencies, containerization, process start, or infra:
- examples: README, requirements, pyproject, package.json, Dockerfile, docker-compose,
  Procfile, .env / .env.example, start scripts, runtime.txt, Makefile, Terraform (*.tf),
  Helm charts, Kubernetes manifests (yaml), cloudbuild, workflows, etc.

IMPORTANT RULES:
- Only return paths that EXIST in the provided list.
- Prefer root-level files if duplicates exist deeper.
- Output STRICT JSON: an array of strings (no prose).

Repo: {repo_url}

Paths:
{paths_text}
"""
    raw = _chat([
        {"role":"system","content":"You are a meticulous DevOps analyst. Output valid JSON only."},
        {"role":"user","content":prompt.strip()}
    ])
    # Extract JSON array
    m = re.search(r"\[.*\]", raw, flags=re.S)
    if not m:
        # fallback: pick top root-level files heuristically (no hard-coded names)
        roots = [p for p in all_paths if "/" not in p][:MAX_FILES_TO_FETCH]
        return roots
    try:
        picked = json.loads(m.group(0))
        # Keep order and ensure they are in all_paths
        valid = [p for p in picked if isinstance(p,str) and p in all_paths]
        return valid[:MAX_FILES_TO_FETCH]
    except Exception:
        roots = [p for p in all_paths if "/" not in p][:MAX_FILES_TO_FETCH]
        return roots

def summarize_with_llm(repo_url, files_payload):
    """
    Ask the model to output a STRICT JSON deployment spec (same contract as before).
    We do NOT dump the directory tree; we only include chosen file snippets.
    """
    snippets=[]
    for path,text in files_payload:
        safe=textwrap.shorten(text, 6000, placeholder="\n[...truncated...]")
        snippets.append(f"### {path}\n```\n{safe}\n```")
    prompt=f"""
Given the repository below, produce a STRICT JSON object with fields:
- provider: "gcp"
- project_id: null if unknown
- region: default "us-central1" if unsure
- zone: default "us-central1-a" if unsure
- language: runtime (e.g., python, node)
- port: integer (guess if needed; 8000 default ok)
- start_command: exact command that runs the app binding 0.0.0.0:<port>
- env: object of key->string for needed env vars (may be empty)

Repo: {repo_url}

Key Files (trimmed):
{chr(10).join(snippets)}

Respond ONLY with valid JSON.
"""
    raw = _chat([
        {"role":"system","content":"You are a senior DevOps engineer. Output strict JSON only."},
        {"role":"user","content":prompt.strip()}
    ])
    # Return raw JSON string (contract expected by gcp_call.py)
    # (we do not validate here; gcp_call.py handles fallback if needed)
    return raw

# --------------------------
def inspect_repo_and_ask(repo_url:str):
    """
    RETURNS:
      tree_md: now a concise human summary line (not the raw tree)
      files_payload: list[(path, text)]
      analysis: STRICT JSON string for the deployment spec
    """
    owner,repo,ref=parse_github_url(repo_url)
    if ref is None:
        ref=resolve_default_branch(owner,repo)
    _, tree=get_tree(owner,repo,ref)

    # make a flat list of file paths for the model to choose from
    all_paths = [t["path"] for t in tree if t.get("type")=="blob"]
    critical_paths = llm_choose_critical_paths(repo_url, all_paths)

    # fetch only the chosen files
    files=[]
    for path in critical_paths:
        raw=fetch_raw(owner,repo,ref,path)
        if not raw: continue
        try: txt=raw.decode("utf-8",errors="replace")
        except Exception: txt="[binary content]"
        files.append((path,txt))

    # build the short human message that gcp_call.py will print where it used to print the tree
    if critical_paths:
        summary_line = "I read the repo and the following files likely contained critical environment/deployment info:\n- " + "\n- ".join(critical_paths)
    else:
        summary_line = "I read the repo, but did not find obvious environment/deployment files. Proceeding with defaults."

    # ask the model for the strict JSON deployment spec (unchanged contract)
    analysis = summarize_with_llm(repo_url, files)

    # IMPORTANT: return the concise summary instead of the full directory tree
    return summary_line, files, analysis

# For standalone testing
if __name__=="__main__":
    repo="https://github.com/Arvo-AI/hello_world"
    summary,files,analysis=inspect_repo_and_ask(repo)
    print("=== Summary ==="); print(summary)
    print("\n=== LLM JSON Spec ==="); print(analysis)
