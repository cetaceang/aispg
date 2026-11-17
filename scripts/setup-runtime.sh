#!/usr/bin/env bash
# Install userspace WARP tools and compile the browserless local WAA signer.
set -euo pipefail
cd "$(dirname "$0")/.."
bash scripts/setup-env.sh
uv sync
uv run python -m aistudio_api.infrastructure.warp.binaries --directory "${AISTUDIO_WARP_DIR:-$PWD/data/warp}/bin"
bash waa-signer/build.sh
printf '\nRuntime ready. Start with: uv run python main.py server\n'
