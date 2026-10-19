#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 tests/test_watson_ideas.py
python3 tests/test_watson_queue_steward.py
export WATSON_GIT_SHA="$(git rev-parse HEAD)"
docker compose up -d --build
