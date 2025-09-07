#!/bin/bash
set -euxo pipefail

mkdir -p /var/log/autodeploy
exec > >(tee -a /var/log/autodeploy/startup.log) 2>&1

# Base tools
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io git curl ca-certificates python3 python3-venv python3-pip
systemctl enable --now docker || true

IMAGE="docker.io/cockckd/hello_world:latest"
PLATFORM=""
ARCH_OK=0
PULL_OK=0

# Env file
install -d -m 0755 /opt
cat > /opt/app.env <<'EOF'
PORT=5000
HOST=0.0.0.0
EOF

# Pull (multi-arch aware)
if [ -n "$IMAGE" ]; then
  if docker manifest inspect "$IMAGE" >/dev/null 2>&1 &&      docker manifest inspect "$IMAGE" | grep -q '"architecture": "amd64"'; then
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
  if docker run -d --name autodeploy-app $PLATFORM --env-file /opt/app.env -p 80:5000 -w /app        --entrypoint /bin/sh "$IMAGE" -lc 'cd app'; then
    exit 0
  fi
  echo "[warn] Explicit start failed; trying image default CMD/ENTRYPOINT"
  docker rm -f autodeploy-app || true
  if docker run -d --name autodeploy-app $PLATFORM --env-file /opt/app.env -p 80:5000 "$IMAGE"; then
    exit 0
  fi
  echo "[warn] Pulled image failed to run; falling back to source."
fi

# Fallback: build/run from source
echo "[info] Cloning and running from source"
rm -rf /opt/app-src
git clone --depth=1 "https://github.com/Arvo-AI/hello_world" /opt/app-src

if [ -f /opt/app-src/Dockerfile ]; then
  echo "[info] Dockerfile present; building local image"
  docker build -t autodeploy-local:latest /opt/app-src
  docker rm -f autodeploy-app || true
  if docker run -d --name autodeploy-app --env-file /opt/app.env -p 80:5000 -w /app        --entrypoint /bin/sh autodeploy-local:latest -lc 'cd app'; then
    exit 0
  fi
  echo "[warn] Local image still failed; trying default CMD"
  docker rm -f autodeploy-app || true
  docker run -d --name autodeploy-app --env-file /opt/app.env -p 80:5000 autodeploy-local:latest
  exit 0
fi

# Native run (no Dockerfile)
echo "[warn] No Dockerfile; running natively via systemd"
if echo "python" | grep -Eiq '^python'; then
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
ExecStart=/bin/bash -lc 'cd app'
Restart=always
User=root
[Install]
WantedBy=multi-user.target
SERVICE
  systemctl daemon-reload
  systemctl enable --now autodeploy.service
  exit 0
fi

if echo "python" | grep -Eiq '^node'; then
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
ExecStart=/bin/bash -lc 'cd app'
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
