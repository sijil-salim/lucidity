#!/usr/bin/env bash
# One-time foundation. READ BEFORE RUNNING - it creates IAM roles and StackSets.
#
# Run from the Organizations MANAGEMENT account (or a delegated StackSets admin,
# add --call-as DELEGATED_ADMIN). The ops/monitoring account is where the
# Ansible controller lives; steps 1-2 must run with credentials for THAT account.
#
#   ORG_ID=o-abc123def4 ORG_ROOT_ID=r-abcd REGIONS="us-east-1 ap-south-1" \
#   ALERT_EMAIL=oncall@example.com ./scripts/deploy_foundation.sh
set -euo pipefail
: "${ORG_ID:?set ORG_ID}" "${ORG_ROOT_ID:?set ORG_ROOT_ID (or an OU id)}"
REGIONS="${REGIONS:-us-east-1 ap-south-1}"
HOME_REGION="${REGIONS%% *}"
CFN=cloudformation

echo "== 1. [ops account] controller identity"
aws cloudformation deploy --region "$HOME_REGION" --stack-name ansible-controller \
  --template-file $CFN/controller.yaml --capabilities CAPABILITY_NAMED_IAM
CONTROLLER_ROLE_ARN=$(aws cloudformation describe-stacks --region "$HOME_REGION" --stack-name ansible-controller \
  --query "Stacks[0].Outputs[?OutputKey=='ControllerRoleArn'].OutputValue" --output text)

echo "== 2. [ops account] per-region sink + SNS topic + dashboard"
declare -A SINK BUS
out() { aws cloudformation describe-stacks --region "$1" --stack-name "$2" \
          --query "Stacks[0].Outputs[?OutputKey=='$3'].OutputValue" --output text; }
for r in $REGIONS; do
  aws cloudformation deploy --region "$r" --stack-name disk-monitoring-hub \
    --template-file $CFN/monitoring-regional.yaml \
    --parameter-overrides OrgId="$ORG_ID" AlertEmail="${ALERT_EMAIL:-}"
  SINK[$r]=$(out "$r" disk-monitoring-hub SinkArn)
  TOPIC=$(out "$r" disk-monitoring-hub AlertsTopicArn)
  # event-driven enrollment: central bus + queue (+ alarm if enrollment gets stuck)
  aws cloudformation deploy --region "$r" --stack-name disk-monitoring-enrollment \
    --template-file $CFN/enrollment-events.yaml --capabilities CAPABILITY_IAM \
    --parameter-overrides OrgId="$ORG_ID" AlertsTopicArn="$TOPIC"
  BUS[$r]=$(out "$r" disk-monitoring-enrollment EventBusArn)
  echo "   queue url for $r: $(out "$r" disk-monitoring-enrollment QueueUrl)"
done

echo "== 3. [org] StackSet: global role (once per account) - auto-deploys to NEW accounts"
aws cloudformation create-stack-set --region "$HOME_REGION" --stack-set-name disk-mon-member-global \
  --template-body file://$CFN/member-global.yaml --capabilities CAPABILITY_NAMED_IAM \
  --permission-model SERVICE_MANAGED --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false \
  --parameters ParameterKey=ControllerRoleArn,ParameterValue="$CONTROLLER_ROLE_ARN" ParameterKey=OrgId,ParameterValue="$ORG_ID"
aws cloudformation create-stack-instances --region "$HOME_REGION" --stack-set-name disk-mon-member-global \
  --deployment-targets OrganizationalUnitIds="$ORG_ROOT_ID" --regions "$HOME_REGION" \
  --operation-preferences FailureToleranceCount=2,MaxConcurrentCount=10

echo "== 4. [org] StackSet: regional pieces (bucket, DHMC, OAM link) - wait for step 3 to finish first"
aws cloudformation create-stack-set --region "$HOME_REGION" --stack-set-name disk-mon-member-regional \
  --template-body file://$CFN/member-regional.yaml --capabilities CAPABILITY_IAM \
  --permission-model SERVICE_MANAGED --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false \
  --parameters ParameterKey=MonitoringSinkArn,ParameterValue="${SINK[$HOME_REGION]}"
for r in $REGIONS; do
  aws cloudformation create-stack-instances --region "$HOME_REGION" --stack-set-name disk-mon-member-regional \
    --deployment-targets OrganizationalUnitIds="$ORG_ROOT_ID" --regions "$r" \
    --parameter-overrides ParameterKey=MonitoringSinkArn,ParameterValue="${SINK[$r]}" \
                          ParameterKey=EventBusArn,ParameterValue="${BUS[$r]}" \
    --operation-preferences FailureToleranceCount=2,MaxConcurrentCount=10
done
echo "Done. Next: on the controller install the enrollment worker + hourly safety-net (see README 'Event-driven enrollment'),"
echo "then: python inventory/generate_inventory.py --from-org"
