#!/usr/bin/env bash
set -euo pipefail

# Builds and pushes the production image to AWS ECR.

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Error: required command '$1' not found in PATH." >&2
    exit 1
  fi
}

require_cmd aws
require_cmd docker

REGION=${AWS_REGION:-us-west-2}
ACCOUNT_ID=${AWS_ACCOUNT_ID:-339713015370}
REPOSITORY=${ECR_REPOSITORY:-aistudioproxy-prod}
IMAGE_TAG=${IMAGE_TAG:-prod}
DOCKERFILE=${DOCKERFILE:-docker/Dockerfile.prod}
PLATFORM=${PLATFORM:-linux/amd64}
CONTEXT_DIR=${CONTEXT_DIR:-.}

REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
LOCAL_IMAGE="${REPOSITORY}:${IMAGE_TAG}"
REMOTE_IMAGE="${REGISTRY}/${REPOSITORY}:${IMAGE_TAG}"

echo "Logging in to ${REGISTRY}..."
aws ecr get-login-password --region "${REGION}" | docker login --username AWS --password-stdin "${REGISTRY}"

echo "Building ${LOCAL_IMAGE} for ${PLATFORM} with ${DOCKERFILE}..."
docker buildx build \
  --platform "${PLATFORM}" \
  -f "${DOCKERFILE}" \
  -t "${LOCAL_IMAGE}" \
  --load \
  "${CONTEXT_DIR}"

echo "Tagging image as ${REMOTE_IMAGE}..."
docker tag "${LOCAL_IMAGE}" "${REMOTE_IMAGE}"

echo "Pushing ${REMOTE_IMAGE}..."
docker push "${REMOTE_IMAGE}"

echo "Image pushed to ${REMOTE_IMAGE}"
