# Independent Watson deployment

Source: `/var/www/watson.backyardbrains.com`.
Runtime: `/var/lib/byb/watson-runtime` (private, not Git).
Secrets: `.env` (mode 0600, not Git).

Memory stays in its separate repository and systemd service at
`/var/www/memory.backyardbrains.com`; PostgreSQL remains its source of truth.
Watson supplies task workflows and MCP tools over that API, plus its worker queue.

Build/restart only Watson: `./deploy.sh` (runs isolated tests and sets source revision).
Do not run the retired OpenClaw compose stack.
The old OpenClaw path is a compatibility symlink for existing BYB timers and
containers. Those routines and their private state still need this runtime;
removing the compatibility path requires migrating each consumer first.

The standalone source was imported from the running server on 2026-10-19,
including queue, capture-quality, task-state and ideas modules missing upstream.

Local isolated checks: `python3 tests/test_watson_ideas.py` and
`python3 tests/test_watson_queue_steward.py` and
`python3 tests/test_project_status_overrides.py` and
`python3 tests/test_task_answers.py`. These use temporary SQLite databases.

## Right brain

`right_brain/` holds the scheduled Claude routine that keeps Today, tasks and
preparation true, and `rb`, the one command it may run. See
`right_brain/README.md` for install, the model switch file and logs.

## What chat assistants get

`get_memory_brief` (MCP) returns the brief from Memory `/v1/brief`: Greg's
standing rules and vocabulary in full, plus the rules and records that match
the request. The complete rule packet is still `get_memory_context`. `publish_to_share` writes
a page under `share.backyardbrains.com/greg/` through the share mount at
`/data/workspaces/main/share`.
