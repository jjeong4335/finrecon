#!/usr/bin/env bash
# Rotate both PostgreSQL passwords on the running instance and redact any
# password that reached the first-boot log (instances launched before the
# user_data.sh fix traced secret generation with set -x).
# New values are generated on the server and never printed.
set -euo pipefail
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
REGION="${REGION:-us-east-1}"
NAME="finrecon-demo"
KEY_FILE="$HOME/.ssh/${NAME}.pem"
IP=$(aws --region "$REGION" --output text ec2 describe-instances \
  --filters "Name=tag:Project,Values=$NAME" "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].PublicIpAddress')
[ "$IP" != "None" ] || { echo "No running finrecon-demo instance in $REGION"; exit 1; }
ssh -o StrictHostKeyChecking=accept-new -i "$KEY_FILE" "ec2-user@$IP" 'sudo bash -s' <<'REMOTE'
set -euo pipefail
cd /opt/finrecon
OWNER=$(openssl rand -hex 16)
RO=$(openssl rand -hex 16)
# Passwords go to psql on stdin, not on a command line (not visible in ps).
docker compose exec -T db psql -q -v ON_ERROR_STOP=1 -U finrecon_owner -d finrecon >/dev/null <<SQL
ALTER ROLE finrecon_owner PASSWORD '${OWNER}';
ALTER ROLE dashboard_ro PASSWORD '${RO}';
SQL
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=${OWNER}/; s/^DASHBOARD_DB_PASSWORD=.*/DASHBOARD_DB_PASSWORD=${RO}/" .env
chmod 600 .env
unset OWNER RO
docker compose up -d --no-build dashboard caddy >/dev/null 2>&1
n=$(grep -cE 'PASSWORD=[0-9a-f]{32}' /var/log/cloud-init-output.log || true)
sed -i -E 's/(PASSWORD=)[0-9a-f]{32}/\1[REDACTED]/g' /var/log/cloud-init-output.log
echo "Passwords rotated. Redacted $n line(s) in cloud-init-output.log."
REMOTE
URL="https://$(echo "$IP" | tr . -).sslip.io"
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w "%{http_code}" "$URL/readyz" || true)
  [ "$code" = "200" ] && break
  sleep 3
done
echo "HTTPS readiness (/readyz): $code"
[ "$code" = "200" ] || { curl -s "$URL/readyz" || true; echo; exit 1; }
