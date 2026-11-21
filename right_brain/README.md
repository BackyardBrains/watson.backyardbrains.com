# Watson's right brain

A scheduled, headless Claude session on the app server that keeps Cortex Today,
Greg's task list and the next 14 days of preparation true. It replaces the
OpenClaw-era routines that last ran on Sep 9, 2026.

| File | What it is |
|---|---|
| `routine.md` | The instructions the session follows on every run. |
| `rb.py` | The only command the session can run. Reads Watson, Gmail and Calendar; writes tasks, answers, notes, Gmail drafts, share pages and Today. It cannot send email, read files, run other commands or change a project's lifecycle. |
| `run.sh` | Starts one run with `claude -p`, restricted to `rb`, and logs the result. |
| `right-brain.env.example` | Model, effort, spending cap and dry-run switch. Copy to `/etc/byb/right-brain.env`. |
| `systemd/` | The service and timer (7:15am daily, 5:15pm weekdays, Detroit time). |

## Install on app

```sh
ln -sf /var/www/watson.backyardbrains.com/right_brain/rb.py /usr/local/bin/rb
install -m 600 right_brain/right-brain.env.example /etc/byb/right-brain.env
cp right_brain/systemd/byb-right-brain.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now byb-right-brain.timer
```

Run once by hand with `right_brain/run.sh`. Rehearse without writing anything
with `RIGHT_BRAIN_DRY_RUN=1 right_brain/run.sh`.

## Where to look

- `/var/lib/byb/right-brain/logs/runs.log`: one line per run with cost, turns and what it wrote.
- `/var/lib/byb/right-brain/logs/<run>.json`: the session's final report.
- `/var/lib/byb/right-brain/runs/<run>.jsonl`: every `rb` write, with its reason.

## Why it has one tool

The session reads email, and email can contain instructions written by anyone.
With a shell, one hostile message could run commands as root. With only `rb`,
the worst a hostile message can do is cause a wrong task, note or unsent draft,
all of which are logged and reversible.

One writer for Today: `rb dashboard` publishes Watson's packet as
`server-claude:right-brain`. Any other publisher that writes after it replaces
it until the next run, so only one job should publish Red items.
