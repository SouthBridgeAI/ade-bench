#!/bin/bash
# The hankweave agent's per-task runner. Copied (as a real file, NOT generated) to
# /installed-agent by HankweaveAgent.perform_task, alongside hw-metrics.js; the hank itself
# (hank.json + prompt-header.md) is synced from the project-root hanks/ade-bench/<hank> into $HANK_DIR.
# ade wraps the run command in `2>&1 | tee`, so this prints the metrics JSON LAST (ade reads the
# last '{...input_tokens...}' line). bun + hankweave are baked into the base image.
set -uo pipefail
export PATH="/usr/local/bin:$HOME/.bun/bin:$PATH"
# Claude Code (spawned by hankweave with --dangerously-skip-permissions) refuses to bypass
# permissions as root unless IS_SANDBOX=1 — and the ade container runs as root.
export IS_SANDBOX=1
unset ANTHROPIC_BASE_URL OPENAI_BASE_URL 2>/dev/null || true

APP="${HANKWEAVE_APP_DIR:-/app}"
EXEC=/tmp/hwexec
DATA=/tmp/hwdata
PROMPT_FILE="${BENCH_TASK_PROMPT_FILE:-/tmp/hw_task_prompt.txt}"
MODEL="${HANKWEAVE_MODEL:-haiku}"

# The hank dir synced from the project-root hanks/ade-bench/<hank> (hank.json + prompt-header.md).
HANK_DIR="${HANKWEAVE_HANK_DIR:-/installed-agent/hank}"
HANK="$HANK_DIR/hank.json"
HEADER="$HANK_DIR/prompt-header.md"

rm -rf "$EXEC" "$DATA"
mkdir -p "$DATA"
cp -a "$APP"/. "$DATA"/ 2>/dev/null || true

# Compose the codon prompt = static header + per-task instructions, written next to hank.json so
# the hank's promptFile "./prompt.md" resolves.
cat "$HEADER" "$PROMPT_FILE" > "$HANK_DIR/prompt.md"

HW="$(command -v hankweave || true)"
if [ -z "$HW" ]; then HW="bunx hankweave@0.6.2"; fi
echo "hankweave: $HW | model: $MODEL | app: $APP | hank: $HANK"

# shellcheck disable=SC2086
$HW "$HANK" "$DATA" \
  --execution "$EXEC" --model "$MODEL" -y --headless || true

# Mirror the agent's edited copy back into the graded /app (handles edits, additions, renames,
# deletions). Guarded so a no-op/failed hank leaves /app untouched (scored on the original).
SRC="$EXEC/agentRoot/project"
if [ -d "$SRC" ] && [ -n "$(ls -A "$SRC" 2>/dev/null)" ]; then
  find "$APP" -mindepth 1 -delete 2>/dev/null || true
  cp -a "$SRC"/. "$APP"/ 2>/dev/null || true
  echo "hankweave: synced $SRC -> $APP"
else
  echo "hankweave: no project output produced; leaving $APP unchanged"
fi

# Stage the full execution dir onto the host log mount (/logs == the trial's sessions/ dir) so the
# bench adapter can preserve it under runs/<run-id>/ and fold server.log into bench.log. Files only
# (no stdout) so the metrics line below stays the last parseable line.
LOGDIR="${BENCH_HARNESS_LOGS_DIR:-/logs}"
if [ -d "$LOGDIR" ] && [ -d "$EXEC" ]; then
  rm -rf "$LOGDIR/hankweave-exec"
  cp -a "$EXEC" "$LOGDIR/hankweave-exec" 2>/dev/null || true
  rm -f "$LOGDIR/hankweave-exec/agentRoot/read_only_data_source" 2>/dev/null || true
  echo "hankweave: staged execution dir to $LOGDIR/hankweave-exec"
fi

node /installed-agent/hw-metrics.js "$EXEC/.hankweave/state.json"
