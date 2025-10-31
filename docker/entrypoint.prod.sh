#!/usr/bin/env bash
set -euo pipefail

log() {
  echo "[entrypoint] $*"
}

PROFILE_BACKEND="${PROFILE_BACKEND:-local}"
LOCAL_PROFILE_DIR="${LOCAL_PROFILE_DIR:-auth_profiles/active}"
CACHE_DIR="${AUTH_PROFILE_CACHE_DIR:-/tmp/auth_profiles}"

log "Selected profile backend: ${PROFILE_BACKEND}"

HYDRATE_ARGS=(--backend "${PROFILE_BACKEND}" --cache-dir "${CACHE_DIR}")
if [[ "${PROFILE_BACKEND}" == "local" ]]; then
  HYDRATE_ARGS+=(--profiles "${LOCAL_PROFILE_DIR}")
else
  if [[ -z "${AUTH_PROFILE_S3_BUCKET:-}" ]]; then
    log "AUTH_PROFILE_S3_BUCKET must be set when PROFILE_BACKEND=s3"
    exit 1
  fi
  HYDRATE_ARGS+=(--bucket "${AUTH_PROFILE_S3_BUCKET}")
  if [[ -n "${AUTH_PROFILE_S3_PREFIX:-}" ]]; then
    HYDRATE_ARGS+=(--prefix "${AUTH_PROFILE_S3_PREFIX}")
  fi
  if [[ -n "${AUTH_PROFILE_S3_REGION:-}" ]]; then
    HYDRATE_ARGS+=(--region "${AUTH_PROFILE_S3_REGION}")
  fi
fi

log "Hydrating auth profiles..."
HYDRATION_JSON="$(python -m coordinator.profiles "${HYDRATE_ARGS[@]}")"
if [[ -z "${HYDRATION_JSON}" ]]; then
  log "Hydration failed to produce output; aborting."
  exit 1
fi

PROFILES_DIR="$(python -c 'import json, sys; data=json.loads(sys.argv[1]); print(data["profiles_dir"])' "${HYDRATION_JSON}")"
KEY_FILE_PATH="$(python -c 'import json, sys; data=json.loads(sys.argv[1]); print(data.get("key_file") or "")' "${HYDRATION_JSON}")"

log "Profiles available at ${PROFILES_DIR}"
if [[ -n "${KEY_FILE_PATH}" ]]; then
  export AUTH_KEY_FILE_PATH="${KEY_FILE_PATH}"
  log "API key file resolved to ${KEY_FILE_PATH}"
else
  unset AUTH_KEY_FILE_PATH || true
  log "No API key file hydrated; API key enforcement disabled."
fi

# Ensure the coordinator treats the hydrated directory as local to avoid re-hydration.
export PROFILE_BACKEND="local"
export AUTH_PROFILE_CACHE_DIR="${CACHE_DIR}"

COORDINATOR_ARGS=(
  "--profiles" "${PROFILES_DIR}"
  "--coordinator-host" "${COORDINATOR_HOST:-0.0.0.0}"
  "--coordinator-port" "${COORDINATOR_PORT:-2048}"
  "--base-api-port" "${BASE_API_PORT:-3100}"
  "--base-stream-port" "${BASE_STREAM_PORT:-3200}"
  "--base-camoufox-port" "${BASE_CAMOUFOX_PORT:-9222}"
  "--port-step" "${PORT_STEP:-1}"
)

if [[ -n "${COORDINATOR_LOG_DIR:-}" ]]; then
  COORDINATOR_ARGS+=("--log-dir" "${COORDINATOR_LOG_DIR}")
fi

if [[ "${HEADLESS:-true}" != "true" ]]; then
  COORDINATOR_ARGS+=("--no-headless")
fi

log "Launching coordinator on ${COORDINATOR_HOST:-0.0.0.0}:${COORDINATOR_PORT:-2048}"
exec python -m coordinator.main "${COORDINATOR_ARGS[@]}"
