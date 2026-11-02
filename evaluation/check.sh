#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest -q test_ocean_chatbot.py evaluation.test_evaluation
exec python3 evaluation/run.py "$@"
