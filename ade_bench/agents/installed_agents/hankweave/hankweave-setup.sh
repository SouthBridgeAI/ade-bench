#!/bin/bash
# Installed once per task by ade: copied into the container as a file and `source`d (NOT wrapped
# with `2>&1 | tee`), so heredocs here are safe. Writes the 1-codon hank + the helper scripts
# that run-hankweave.sh uses. bun + hankweave are baked into the base image.
set -e
echo "Setup hankweave agent"
export PATH="/usr/local/bin:$HOME/.bun/bin:$PATH"

if command -v bun >/dev/null 2>&1; then
  echo "bun: $(bun --version)"
else
  echo "ERROR: bun not found on PATH — rebuild the ade base image with bun baked in" >&2
fi

mkdir -p /installed-agent

# --- The hank: stage the read-only dbt snapshot into an editable agentRoot/project, then the
# haiku codon edits it in place. (Verified: rig workingDirectory "project" runs in agentRoot,
# where read_only_data_source lives.) Model is overridden at runtime via --model. ---
cat > /installed-agent/hank.json <<'HANKEOF'
{
  "meta": { "name": "ade-hankweave-haiku", "version": "1.0.0" },
  "hank": [
    {
      "id": "fix",
      "name": "Fix dbt project",
      "model": "haiku",
      "continuationMode": "fresh",
      "env": { "IS_SANDBOX": "1" },
      "promptFile": "./prompt.md",
      "rigSetup": [
        {
          "type": "command",
          "command": {
            "run": "mkdir -p project && cp -a read_only_data_source/. project/",
            "workingDirectory": "project"
          }
        }
      ]
    }
  ]
}
HANKEOF

# --- Prompt header; the per-task instructions are appended at runtime. ---
cat > /installed-agent/hw-prompt-header.md <<'PROMPTEOF'
You are an expert analytics engineer working on a dbt project.

The dbt project is in the `project/` directory inside your current working directory. It is a
full, editable copy of the project. Modify files **in place** under `project/` to accomplish the
task described below.

Rules:
- Keep dbt paths/filenames valid (models live under `project/models/`, config in
  `project/dbt_project.yml`, etc.).
- Do NOT write outside `project/`, and do NOT delete the whole project. The final, corrected
  project must remain at `project/`.
- Use your shell and file-editing tools to inspect and change files. If you need to test, run
  dbt commands from inside `project/`.

TASK:
PROMPTEOF

# --- Runner: snapshot /app -> read-only data source, run the hank, mirror the edited tree back
# into /app, then print the metrics JSON LAST (ade reads the last '{...input_tokens...}' line). ---
cat > /installed-agent/run-hankweave.sh <<'RUNEOF'
#!/bin/bash
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

rm -rf "$EXEC" "$DATA"
mkdir -p "$DATA"
cp -a "$APP"/. "$DATA"/ 2>/dev/null || true

cat /installed-agent/hw-prompt-header.md "$PROMPT_FILE" > /installed-agent/prompt.md

HW="$(command -v hankweave || true)"
if [ -z "$HW" ]; then HW="bunx hankweave@0.6.2"; fi
echo "hankweave: $HW | model: $MODEL | app: $APP"

# shellcheck disable=SC2086
$HW /installed-agent/hank.json "$DATA" \
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

node /installed-agent/hw-metrics.js "$EXEC/.hankweave/state.json"
RUNEOF

# --- Metrics: read hankweave state.json, sum codon cost/tokens, print one compact JSON line.
# Field names verified against hankweave 0.6.x state.json. ---
cat > /installed-agent/hw-metrics.js <<'METRICEOF'
const fs = require("fs");
const path = process.argv[2];
const out = {
  input_tokens: 0,
  output_tokens: 0,
  cache_tokens: 0,
  num_turns: 1,
  runtime_ms: 0,
  cost_usd: 0.0,
};
try {
  const s = JSON.parse(fs.readFileSync(path, "utf8"));
  const runs = s.runs || [];
  const run = runs[runs.length - 1] || {};
  for (const c of run.codons || []) {
    const t = c.finalTokens || c.currentTokens || {};
    out.input_tokens += t.inputTokens || 0;
    out.output_tokens += t.outputTokens || 0;
    out.cache_tokens += (t.cacheCreationTokens || 0) + (t.cacheReadTokens || 0);
    const cost =
      c.finalCost != null ? c.finalCost : c.currentCost != null ? c.currentCost : c.partialCost;
    out.cost_usd += cost || 0;
  }
} catch (e) {
  // No/invalid state.json (e.g. hankweave failed before writing) — still emit a parseable line.
}
console.log(JSON.stringify(out));
METRICEOF

echo "hankweave agent ready"
