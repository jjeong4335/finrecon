#!/usr/bin/env bash
# One-command deployment of the finrecon demo to a single EC2 instance.
#
#   ./deploy/aws/launch.sh                      # free sslip.io name derived from the Elastic IP
#   DOMAIN=recon.example.com ./deploy/aws/launch.sh   # your own DNS name (point an A record at the IP)
#
# Creates (all tagged Project=finrecon-demo, removed by teardown.sh):
#   security group (80/443 from anywhere, 22 from your current IP only), key pair
#   (~/.ssh/finrecon-demo.pem), Elastic IP, one Amazon Linux 2023 arm64 instance.
# Requires: AWS CLI v2 configured (aws configure / aws sso login).
set -euo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
REGION="${REGION:-us-east-1}"
TYPE="${INSTANCE_TYPE:-t4g.small}"
NAME="finrecon-demo"
REPO="${REPO:-https://github.com/jjeong4335/finrecon.git}"
KEY_FILE="$HOME/.ssh/${NAME}.pem"
HERE="$(cd "$(dirname "$0")" && pwd)"
TAG="ResourceType=%s,Tags=[{Key=Name,Value=${NAME}},{Key=Project,Value=${NAME}}]"
aws_() { aws --region "$REGION" --output text "$@"; }

command -v aws >/dev/null || { echo "AWS CLI not found: brew install awscli && aws configure"; exit 1; }
ACCOUNT=$(aws_ sts get-caller-identity --query Account) || { echo "AWS CLI is not authenticated: run aws configure"; exit 1; }
echo "Account $ACCOUNT, region $REGION, instance type $TYPE"

EXISTING=$(aws_ ec2 describe-instances --filters "Name=tag:Project,Values=$NAME" \
  "Name=instance-state-name,Values=pending,running,stopping,stopped" --query 'Reservations[].Instances[].InstanceId')
if [ -n "$EXISTING" ]; then
  echo "An instance already exists: $EXISTING. Run ./deploy/aws/status.sh, or teardown.sh first."; exit 1
fi

VPC=$(aws_ ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId')
[ "$VPC" != "None" ] || { echo "No default VPC in $REGION"; exit 1; }
MY_IP=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')

SG=$(aws_ ec2 describe-security-groups --filters "Name=group-name,Values=$NAME" "Name=vpc-id,Values=$VPC" \
  --query 'SecurityGroups[0].GroupId')
if [ "$SG" = "None" ]; then
  SG=$(aws_ ec2 create-security-group --group-name "$NAME" --vpc-id "$VPC" \
    --description "finrecon demo: HTTP/HTTPS public, SSH from owner IP" \
    --tag-specifications "$(printf "$TAG" security-group)" --query GroupId)
  aws_ ec2 authorize-security-group-ingress --group-id "$SG" --ip-permissions \
    "IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0}]" \
    "IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0}]" \
    "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=${MY_IP}/32,Description=owner}]" >/dev/null
fi
echo "Security group $SG (SSH allowed from $MY_IP only)"

if ! aws_ ec2 describe-key-pairs --key-names "$NAME" >/dev/null 2>&1; then
  mkdir -p "$HOME/.ssh"
  aws_ ec2 create-key-pair --key-name "$NAME" --key-type ed25519 \
    --tag-specifications "$(printf "$TAG" key-pair)" --query KeyMaterial > "$KEY_FILE"
  chmod 600 "$KEY_FILE"
fi
[ -f "$KEY_FILE" ] || echo "Note: key pair exists in AWS but $KEY_FILE is missing; SSH will not work."

read -r ALLOC IP < <(aws_ ec2 allocate-address --domain vpc \
  --tag-specifications "$(printf "$TAG" elastic-ip)" --query '[AllocationId,PublicIp]')
DOMAIN="${DOMAIN:-$(echo "$IP" | tr . -).sslip.io}"
echo "Elastic IP $IP -> https://$DOMAIN"

AMI=$(aws_ ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
  --query Parameter.Value)
USER_DATA=$(mktemp)
sed -e "s#__DOMAIN__#${DOMAIN}#g" -e "s#__REPO__#${REPO}#g" "$HERE/user_data.sh" > "$USER_DATA"

INSTANCE=$(aws_ ec2 run-instances --image-id "$AMI" --instance-type "$TYPE" --key-name "$NAME" \
  --security-group-ids "$SG" --user-data "file://$USER_DATA" \
  --metadata-options HttpTokens=required \
  --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":20,"VolumeType":"gp3","Encrypted":true}}]' \
  --tag-specifications "$(printf "$TAG" instance)" "$(printf "$TAG" volume)" \
  --query 'Instances[0].InstanceId')
rm -f "$USER_DATA"
echo "Instance $INSTANCE starting..."
aws_ ec2 wait instance-running --instance-ids "$INSTANCE"
aws_ ec2 associate-address --instance-id "$INSTANCE" --allocation-id "$ALLOC" >/dev/null

cat <<MSG

Launched. The server is now installing Docker, building the image and loading data
(about 10-15 minutes; the UCI retail load continues in the background after that).

  URL:      https://$DOMAIN
  Progress: ./deploy/aws/status.sh
  SSH:      ssh -i $KEY_FILE ec2-user@$IP
  Remove:   ./deploy/aws/teardown.sh
MSG
