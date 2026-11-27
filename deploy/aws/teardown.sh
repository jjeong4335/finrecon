#!/usr/bin/env bash
# Remove everything launch.sh created (instance, Elastic IP, security group, key pair).
set -euo pipefail
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
REGION="${REGION:-us-east-1}"
NAME="finrecon-demo"
aws_() { aws --region "$REGION" --output text "$@"; }
read -r -p "Delete the finrecon-demo instance and its Elastic IP in $REGION? [y/N] " ok
[ "$ok" = "y" ] || exit 0
IDS=$(aws_ ec2 describe-instances --filters "Name=tag:Project,Values=$NAME" \
  "Name=instance-state-name,Values=pending,running,stopping,stopped" --query 'Reservations[].Instances[].InstanceId')
if [ -n "$IDS" ]; then
  aws_ ec2 terminate-instances --instance-ids $IDS >/dev/null
  aws_ ec2 wait instance-terminated --instance-ids $IDS
  echo "Terminated $IDS"
fi
for A in $(aws_ ec2 describe-addresses --filters "Name=tag:Project,Values=$NAME" --query 'Addresses[].AllocationId'); do
  aws_ ec2 release-address --allocation-id "$A" && echo "Released Elastic IP $A"
done
SG=$(aws_ ec2 describe-security-groups --filters "Name=group-name,Values=$NAME" --query 'SecurityGroups[0].GroupId')
[ "$SG" = "None" ] || { aws_ ec2 delete-security-group --group-id "$SG" && echo "Deleted security group $SG"; }
aws_ ec2 delete-key-pair --key-name "$NAME" >/dev/null 2>&1 && echo "Deleted key pair $NAME (local file ~/.ssh/$NAME.pem kept)"
echo "Done."
