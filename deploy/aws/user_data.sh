#!/bin/bash
# EC2 user data (runs once, as root, on first boot). Rendered by launch.sh, which
# replaces __DOMAIN__ and __REPO__. Log: /var/log/cloud-init-output.log
set -euxo pipefail

# 2 GB swap: headroom for image builds and the 1M-row UCI load on a 2 GB instance.
if [ ! -f /swapfile ]; then
  dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap defaults 0 0' >> /etc/fstab
fi

dnf install -y docker git
systemctl enable --now docker
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

APP=/opt/finrecon
[ -d "$APP/.git" ] || git clone --depth 1 "__REPO__" "$APP"
cd "$APP"

# Secrets are generated on the server and never leave it. Tracing (set -x) is
# switched off here so the generated values never reach cloud-init-output.log.
set +x
if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 16)/" .env
  sed -i "s/^DASHBOARD_DB_PASSWORD=.*/DASHBOARD_DB_PASSWORD=$(openssl rand -hex 16)/" .env
  sed -i "s/^DOMAIN=.*/DOMAIN=__DOMAIN__/" .env
  chmod 600 .env
fi
set -x

# Build the app image once with the classic builder (no buildx plugin needed),
# then start everything. restart: unless-stopped brings it back after reboots.
DOCKER_BUILDKIT=0 docker build -t finrecon-app:latest -f docker/Dockerfile .
docker compose up -d --no-build
echo "finrecon deployment finished: https://__DOMAIN__"
