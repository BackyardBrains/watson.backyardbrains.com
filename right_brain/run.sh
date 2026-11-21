#!/bin/bash
# Run Watson's right brain once: a headless Claude session whose only tool is `rb`.
#
# The session reads email, so it gets no shell and no file access. `--tools Bash`
# with `--allowedTools "Bash(rb:*)"` and `--permission-mode dontAsk` leaves it one
# command it can run; everything else is refused without asking. Settings, hooks
# and MCP servers are switched off so nothing else loads into the session.
#
# The model, effort and spending cap come from /etc/byb/right-brain.env, so they
# can be changed without touching this file. See right-brain.env.example.
set -uo pipefail

HERE=$(dirname "$(readlink -f "$0")")
STATE=${RIGHT_BRAIN_STATE:-/var/lib/byb/right-brain}
[ -f /etc/byb/right-brain.env ] && . /etc/byb/right-brain.env

MODEL=${RIGHT_BRAIN_MODEL:-claude-opus-5-5}
EFFORT=${RIGHT_BRAIN_EFFORT:-high}
MAX_USD=${RIGHT_BRAIN_MAX_USD:-8}
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)

export HOME=/root
export PATH="$HERE/bin:/usr/local/bin:/root/.local/bin:/usr/bin:/bin"
export RB_RUN_LOG="$STATE/runs/$RUN_ID.jsonl"
export RB_DRY_RUN=${RIGHT_BRAIN_DRY_RUN:-}

mkdir -p "$STATE/runs" "$STATE/logs" "$STATE/empty"
chmod 700 "$STATE"
exec 9>"$STATE/lock"
if ! flock -n 9; then
  echo "right brain: another run is still going; skipped $RUN_ID"
  exit 0
fi

PROMPT=$(mktemp)
trap 'rm -f "$PROMPT"' EXIT
{
  cat "$HERE/routine.md"
  printf '\n\nIt is now %s, America/Detroit time. Run id %s.\n' "$(TZ=America/Detroit date '+%a %b %-d, %Y, %-I:%M %p')" "$RUN_ID"
  [ -n "$RB_DRY_RUN" ] && printf 'This is a dry run: rb prints what each write would do and changes nothing. Do the whole routine anyway.\n'
} > "$PROMPT"

cd "$STATE/empty"
claude -p \
  --model "$MODEL" \
  --effort "$EFFORT" \
  --tools "Bash" \
  --allowedTools "Bash(rb:*)" \
  --permission-mode dontAsk \
  --setting-sources "" \
  --strict-mcp-config \
  --no-session-persistence \
  --max-budget-usd "$MAX_USD" \
  --output-format json \
  < "$PROMPT" > "$STATE/logs/$RUN_ID.json" 2> "$STATE/logs/$RUN_ID.err"
RC=$?

python3 - "$STATE/logs/$RUN_ID.json" "$RB_RUN_LOG" "$RC" "$MODEL" "$RUN_ID" <<'PY' | tee -a "$STATE/logs/runs.log"
import json, sys
path, run_log, rc, model, run_id = sys.argv[1:6]
try:
    result = json.load(open(path))
except Exception:
    result = {}
writes = []
try:
    writes = [json.loads(line) for line in open(run_log)]
except Exception:
    pass
done = [w["verb"] for w in writes if "error" not in w and w["verb"] in {"task-new", "task-update", "answer", "note", "project-status", "mail-draft", "share", "dashboard"}]
print(json.dumps({
    "run": run_id, "exit": int(rc), "model": model, "is_error": result.get("is_error"),
    "turns": result.get("num_turns"), "cost_usd": result.get("total_cost_usd"),
    "denied": len(result.get("permission_denials") or []),
    "writes": {verb: done.count(verb) for verb in sorted(set(done))},
    "rb_errors": sum(1 for w in writes if "error" in w),
}))
PY

# A run stopped by the spending cap or an API error can still exit 0.
if [ "$RC" -eq 0 ] && ! python3 -c 'import json,sys; sys.exit(1 if json.load(open(sys.argv[1])).get("is_error") else 0)' "$STATE/logs/$RUN_ID.json"; then
  RC=3
fi
if [ "$RC" -ne 0 ]; then
  echo "right brain run $RUN_ID failed with exit $RC; see $STATE/logs/$RUN_ID.err" >&2
fi
exit "$RC"
