#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${1:-wos-stg}"
REMOTE_PATH="${2:-/home/vietlubu/projects/bear/deepwiki-open}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "==> Repo root: ${REPO_ROOT}"
echo "==> Remote: ${REMOTE_HOST}:${REMOTE_PATH}"

cd "${REPO_ROOT}"

if [[ ! -d node_modules ]]; then
  echo "==> node_modules not found, running npm ci"
  npm ci
fi

echo "==> Building frontend (Next.js standalone)"
npm run build

if [[ ! -f .next/standalone/server.js ]]; then
  echo "Build artifact missing: .next/standalone/server.js"
  exit 1
fi

echo "==> Preparing remote directories"
ssh "${REMOTE_HOST}" "mkdir -p '${REMOTE_PATH}' '${REMOTE_PATH}/.next/static' '${REMOTE_PATH}/public'"

echo "==> Sync standalone server bundle"
rsync -az --delete --exclude='.env' --exclude='.env.*' -e ssh \
  .next/standalone/ "${REMOTE_HOST}:${REMOTE_PATH}/"

echo "==> Sync Next static assets"
rsync -az --delete -e ssh \
  .next/static/ "${REMOTE_HOST}:${REMOTE_PATH}/.next/static/"

echo "==> Sync public assets"
rsync -az --delete -e ssh \
  public/ "${REMOTE_HOST}:${REMOTE_PATH}/public/"

echo "==> Done"
echo "Run on server:"
echo "cd ${REMOTE_PATH} && PORT=3000 SERVER_BASE_URL=http://127.0.0.1:8001 node server.js"
