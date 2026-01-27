#!/usr/bin/env bash
set -euo pipefail

ENV_NAME=$(azd env get-value AZURE_ENV_NAME)
ENV_FILE="$(dirname "$0")/../.azure/${ENV_NAME}/.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "azd environment file not found: $ENV_FILE" >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a
source "$ENV_FILE"
set +a

# Fallbacks used by the app
if [ -z "${AZURE_CONTAINER_APPS_SESSION_POOL_ENDPOINT:-}" ] && [ -n "${DYNAMIC_SESSION_POOL_ENDPOINT:-}" ]; then
  export AZURE_CONTAINER_APPS_SESSION_POOL_ENDPOINT="$DYNAMIC_SESSION_POOL_ENDPOINT"
fi
if [ -z "${AZURE_OPENAI_CHAT_DEPLOYMENT_NAME:-}" ] && [ -n "${AZURE_OPENAI_DEPLOYMENT:-}" ]; then
  export AZURE_OPENAI_CHAT_DEPLOYMENT_NAME="$AZURE_OPENAI_DEPLOYMENT"
fi
if [ -z "${SESSION_POOL_AUDIENCE:-}" ]; then
  export SESSION_POOL_AUDIENCE="https://dynamicsessions.io/.default"
fi

echo "Loaded azd environment variables into current session."
echo "AZURE_OPENAI_ENDPOINT=$AZURE_OPENAI_ENDPOINT"
echo "AZURE_OPENAI_CHAT_DEPLOYMENT_NAME=$AZURE_OPENAI_CHAT_DEPLOYMENT_NAME"
echo "AZURE_CONTAINER_APPS_SESSION_POOL_ENDPOINT=$AZURE_CONTAINER_APPS_SESSION_POOL_ENDPOINT"
