#!/usr/bin/env bash
set -euo pipefail

if [[ "${SKIP_LIVE_LLM_E2E:-0}" == "1" ]]; then
    exit 0
fi

if [[ "${PRE_COMMIT_TO_REF:-}" == "0000000000000000000000000000000000000000" ]]; then
    exit 0
fi

if [[ -f /root/.local/share/agentic/deploy/automations/e2e_live_llm.py ]]; then
    ROOT="$(git rev-parse --show-toplevel)"
    exec python3 /root/.local/share/agentic/deploy/automations/e2e_live_llm.py \
        --suite nalam --for-push "$ROOT" "${PRE_COMMIT_TO_REF:-HEAD}"
fi

exec env AGENTIC_LIVE_LLM_E2E=1 ./venv/bin/python -m pytest -q tests/test_e2e_pipeline.py
