#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deploy_codecommit_lambda.sh
#
# Builds the CodeCommit container Lambda image, pushes it to ECR, and
# creates (or updates) the Lambda function + EventBridge trigger.
#
# Prerequisites:
#   - AWS CLI v2 configured with sufficient permissions
#   - Docker (BuildKit enabled)
#   - jq
#
# Usage:
#   export AWS_REGION=us-east-1
#   export AWS_ACCOUNT_ID=123456789012
#   export REPO_NAME=my-codecommit-repo          # CodeCommit repository name
#   export LAMBDA_ROLE_ARN=arn:aws:iam::...:role/pr-agent-lambda-role
#   export SECRET_ARN=arn:aws:secretsmanager:...:secret:pr-agent-config
#   ./deploy_codecommit_lambda.sh
# ---------------------------------------------------------------------------
set -euo pipefail

: "${AWS_REGION:?Set AWS_REGION}"
: "${AWS_ACCOUNT_ID:?Set AWS_ACCOUNT_ID}"
: "${REPO_NAME:?Set REPO_NAME (CodeCommit repository name)}"
: "${LAMBDA_ROLE_ARN:?Set LAMBDA_ROLE_ARN}"
: "${SECRET_ARN:?Set SECRET_ARN (Secrets Manager ARN for pr-agent config)}"

ECR_REPO="pr-agent-codecommit"
LAMBDA_NAME="pr-agent-codecommit"
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"
IMAGE_TAG="latest"
EVENTBRIDGE_RULE_NAME="pr-agent-codecommit-pr-events"

echo "==> Logging in to ECR"
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

echo "==> Creating ECR repository (idempotent)"
aws ecr describe-repositories --repository-names "${ECR_REPO}" --region "${AWS_REGION}" 2>/dev/null \
  || aws ecr create-repository --repository-name "${ECR_REPO}" --region "${AWS_REGION}"

echo "==> Building container image (target: codecommit_lambda)"
docker build \
  --target codecommit_lambda \
  --platform linux/amd64 \
  -t "${ECR_URI}:${IMAGE_TAG}" \
  -f docker/Dockerfile.lambda \
  .

echo "==> Pushing image to ECR"
docker push "${ECR_URI}:${IMAGE_TAG}"

IMAGE_URI=$(aws ecr describe-images \
  --repository-name "${ECR_REPO}" \
  --region "${AWS_REGION}" \
  --query 'sort_by(imageDetails, &imagePushedAt)[-1].imageDigest' \
  --output text)
FULL_IMAGE_URI="${ECR_URI}@${IMAGE_URI}"

# Lambda environment variables — credentials come from Secrets Manager at runtime
LAMBDA_ENV=$(cat <<EOF
{
  "Variables": {
    "CONFIG__SECRET_PROVIDER": "aws_secrets_manager",
    "CONFIG__GIT_PROVIDER": "codecommit",
    "AWS_SECRETS_MANAGER__REGION_NAME": "${AWS_REGION}",
    "AWS_SECRETS_MANAGER__SECRET_ARN": "${SECRET_ARN}",
    "CONFIG__LOG_LEVEL": "INFO"
  }
}
EOF
)

if aws lambda get-function --function-name "${LAMBDA_NAME}" --region "${AWS_REGION}" 2>/dev/null; then
  echo "==> Updating existing Lambda function code"
  aws lambda update-function-code \
    --function-name "${LAMBDA_NAME}" \
    --image-uri "${ECR_URI}:${IMAGE_TAG}" \
    --region "${AWS_REGION}"

  echo "==> Waiting for update to complete"
  aws lambda wait function-updated --function-name "${LAMBDA_NAME}" --region "${AWS_REGION}"

  echo "==> Updating Lambda environment variables"
  aws lambda update-function-configuration \
    --function-name "${LAMBDA_NAME}" \
    --environment "${LAMBDA_ENV}" \
    --region "${AWS_REGION}"
else
  echo "==> Creating Lambda function"
  aws lambda create-function \
    --function-name "${LAMBDA_NAME}" \
    --package-type Image \
    --code "ImageUri=${ECR_URI}:${IMAGE_TAG}" \
    --role "${LAMBDA_ROLE_ARN}" \
    --environment "${LAMBDA_ENV}" \
    --timeout 300 \
    --memory-size 512 \
    --region "${AWS_REGION}"
fi

echo "==> Waiting for Lambda to be active"
aws lambda wait function-active --function-name "${LAMBDA_NAME}" --region "${AWS_REGION}"

LAMBDA_ARN=$(aws lambda get-function \
  --function-name "${LAMBDA_NAME}" \
  --region "${AWS_REGION}" \
  --query 'Configuration.FunctionArn' \
  --output text)

echo "==> Creating/updating EventBridge rule"
aws events put-rule \
  --name "${EVENTBRIDGE_RULE_NAME}" \
  --event-pattern "{\"source\":[\"aws.codecommit\"],\"detail-type\":[\"CodeCommit Pull Request State Change\"],\"detail\":{\"event\":[\"pullRequestCreated\",\"pullRequestSourceBranchUpdated\"],\"repositoryNames\":[\"${REPO_NAME}\"]}}" \
  --state ENABLED \
  --region "${AWS_REGION}"

echo "==> Granting EventBridge permission to invoke Lambda"
aws lambda add-permission \
  --function-name "${LAMBDA_NAME}" \
  --statement-id "EventBridgeInvoke" \
  --action "lambda:InvokeFunction" \
  --principal "events.amazonaws.com" \
  --source-arn "arn:aws:events:${AWS_REGION}:${AWS_ACCOUNT_ID}:rule/${EVENTBRIDGE_RULE_NAME}" \
  --region "${AWS_REGION}" 2>/dev/null || true   # idempotent — ignore "already exists" error

echo "==> Attaching Lambda as EventBridge rule target"
aws events put-targets \
  --rule "${EVENTBRIDGE_RULE_NAME}" \
  --targets "Id=pr-agent-codecommit-target,Arn=${LAMBDA_ARN}" \
  --region "${AWS_REGION}"

echo ""
echo "==> Deployment complete!"
echo "    Lambda:         ${LAMBDA_NAME}"
echo "    ECR image:      ${ECR_URI}:${IMAGE_TAG}"
echo "    EventBridge:    ${EVENTBRIDGE_RULE_NAME}"
echo "    Secrets ARN:    ${SECRET_ARN}"
echo ""
echo "Secrets Manager JSON structure expected at ${SECRET_ARN}:"
cat <<'SECRETS'
{
  "openai.key": "sk-...",
  "config.model": "gpt-4o",
  "config.git_provider": "codecommit"
}
SECRETS
