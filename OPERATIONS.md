# Independent Watson deployment

Source: `/var/www/watson.backyardbrains.com`.
Runtime: `/var/lib/byb/watson-runtime` (private, not Git).
Secrets: `.env` (mode 0600, not Git).

Memory stays in its separate repository and systemd service at
`/var/www/memory.backyardbrains.com`; PostgreSQL remains its source of truth.
Watson supplies task workflows and MCP tools over that API, plus its worker queue.

Build/restart only Watson: `docker compose up -d --build`.
Do not run the retired OpenClaw compose stack.
The old OpenClaw path is a compatibility symlink for existing BYB timers and
containers. Those routines and their private state still need this runtime;
removing the compatibility path requires migrating each consumer first.

The standalone source was imported from the running server on 2026-10-19,
including queue, capture-quality, task-state and ideas modules missing upstream.
