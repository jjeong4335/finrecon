#!/usr/bin/env bash
# Deploy the latest main branch to the running instance: pull, rebuild, restart.
# The database volume and the UCI retail data are kept.
set -euo pipefail
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
REGION="${REGION:-us-east-1}"
NAME="finrecon-demo"
KEY_FILE="$HOME/.ssh/${NAME}.pem"
IP=$(aws --region "$REGION" --output text ec2 describe-instances \
  --filters "Name=tag:Project,Values=$NAME" "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].PublicIpAddress')
[ "$IP" != "None" ] || { echo "No running finrecon-demo instance in $REGION"; exit 1; }
echo "Updating $IP ..."
ssh -o StrictHostKeyChecking=accept-new -i "$KEY_FILE" "ec2-user@$IP" 'set -e
  cd /opt/finrecon
  sudo git pull --ff-only
  sudo DOCKER_BUILDKIT=0 docker build -q -t finrecon-app:latest -f docker/Dockerfile .
  sudo docker compose up -d --no-build --remove-orphans dashboard caddy
  sudo docker compose restart caddy'
URL="https://$(echo "$IP" | tr . -).sslip.io"
# Check from here over HTTPS (plain HTTP on the server answers 308 -> https by design).
for i in $(seq 1 20); do
  code=$(curl -s -o /dev/null -w "%{http_code}" "$URL/readyz" || true)
  [ "$code" = "200" ] && break
  sleep 3
done
echo "HTTPS readiness (/readyz): $code"
echo "Done: $URL"
