#!/usr/bin/env bash
# Show where the deployment stands: instance, URL, setup log tail, container status.
set -euo pipefail
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
REGION="${REGION:-us-east-1}"
NAME="finrecon-demo"
KEY_FILE="$HOME/.ssh/${NAME}.pem"
read -r INSTANCE IP STATE < <(aws --region "$REGION" --output text ec2 describe-instances \
  --filters "Name=tag:Project,Values=$NAME" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[0].Instances[0].[InstanceId,PublicIpAddress,State.Name]')
[ "$INSTANCE" != "None" ] || { echo "No finrecon-demo instance found in $REGION"; exit 1; }
DOMAIN="$(echo "$IP" | tr . -).sslip.io"
echo "Instance $INSTANCE ($STATE), IP $IP"
echo "Default URL: https://$DOMAIN"
printf "HTTPS readiness: "; curl -s -m 10 -o /dev/null -w "%{http_code}\n" "https://$DOMAIN/readyz" || echo "not reachable yet"
if [ -f "$KEY_FILE" ]; then
  ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -i "$KEY_FILE" "ec2-user@$IP" \
    'echo "--- setup log (last 15 lines)"; sudo tail -n 15 /var/log/cloud-init-output.log;
     echo "--- containers"; cd /opt/finrecon 2>/dev/null && sudo docker compose ps -a --format "table {{.Service}}\t{{.State}}\t{{.Status}}" || true;
     echo "--- certificate (caddy)"; sudo docker logs finrecon-caddy-1 2>&1 | grep -iE "certificate obtained|error|could not" | tail -n 5 || true'
fi
