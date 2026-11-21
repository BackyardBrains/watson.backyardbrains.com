#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 tests/test_watson_ideas.py
python3 tests/test_watson_queue_steward.py
python3 tests/test_project_status_overrides.py
python3 tests/test_task_answers.py
python3 tests/test_share_publish.py
python3 tests/test_right_brain_tool.py
export WATSON_GIT_SHA="$(git rev-parse HEAD)"
docker compose up -d --build
