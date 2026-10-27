#!/usr/bin/env bash
# Manual deploy: tests, then Modal deploy, then /health must be 200.
# Usage: ./scripts/deploy.sh   (run from the repo root)
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Running full test suite"
.venv/bin/python -m pytest -q

echo "==> Deploying to Modal"
.venv/bin/modal deploy modal_app.py 2>&1 | tee /tmp/sisi-deploy.log
WEB_URL=$(grep -o 'https://[^[:space:]]*' /tmp/sisi-deploy.log | head -1)
test -n "$WEB_URL"

echo "==> Health check: $WEB_URL/health"
curl -sf "$WEB_URL/health"
echo
echo "OK: deployed and healthy."
