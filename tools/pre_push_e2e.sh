#!/usr/bin/env bash
set -euo pipefail

if [[ "${SKIP_LIVE_LLM_E2E:-0}" == "1" ]]; then
    exit 0
fi

if [[ -f /root/.local/share/agentic/deploy/automations/e2e_live_llm.py ]]; then
    exec python3 /root/.local/share/agentic/deploy/automations/e2e_live_llm.py --suite nalam --for-push "$(git rev-parse --show-toplevel)" HEAD
fi

exec env AGENTIC_LIVE_LLM_E2E=1 ./venv/bin/python -m pytest -q tests/test_e2e_pipeline.py
