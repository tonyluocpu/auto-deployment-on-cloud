#!/bin/bash
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
docker pull None

# Create a simple .env file on the VM
cat > /opt/app.env <<'EOF'
PORT=5000
HOST=0.0.0.0
EOF

# Run the container
docker rm -f autodeploy-app || true
docker run -d --name autodeploy-app --env-file /opt/app.env -p 5000:5000 None

# Forward port 80 to the application port
if command -v iptables >/dev/null 2>&1; then
  iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-ports 5000
  iptables -t nat -A OUTPUT -p tcp --dport 80 -j REDIRECT --to-ports 5000
else
  echo "[warn] iptables not present; port 80 will not forward to 5000."
fi
