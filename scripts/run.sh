#!/usr/bin/env bash
#
# run-experiments.sh - run the HW.md model matrix, one live tmux pane per model.
#
# Container / compose-project names are scoped per run-id (see
# TrialHandler.client_container_name), so every model can run the SAME task
# concurrently without colliding. This script launches each (agent, model) in
# its own tmux pane so you can watch all of them at once. A final "monitor"
# pane waits for every model to finish, prints the pass/fail summary, and then
# cleans up Docker (so cleanup never fires while a run is still going).
#
# Each pane builds its own image on first run (images are shared by task-id, so
# Docker's layer cache dedups the work across panes).
#
# Auth: invokes ./adex, so Claude and Codex run on subscription credentials
# (.claude-credentials.json / .codex-auth.json) and Gemini uses GEMINI_API_KEY.
#
# Usage:
#   scripts/run-experiments.sh [TASKS]
#
#   TASKS   task selector passed to `ade run` (default: all).
#           e.g. "all", "simple001", "airbnb+"
#
# Env overrides:
#   DB=duckdb              database filter
#   PROJECT_TYPE=dbt       project-type filter
#   CONCURRENCY=4          --n-concurrent-trials per model
#   NO_CLEANUP=0           1 = leave Docker containers/images in place
#   TMUX_SESSION=...       tmux session name (default: ade-experiments)
#   NO_ATTACH=0            1 = set up the session but don't attach/switch to it
#
# Examples:
#   scripts/run-experiments.sh simple001     # smoke-test the whole matrix
#   scripts/run-experiments.sh               # full run over all tasks
#   CONCURRENCY=8 scripts/run-experiments.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
SELF="${BASH_SOURCE[0]}"

# (agent, model) pairs from HW.md. Constant; needed in every mode below.
EXPERIMENTS=(
  "gemini:gemini-3.5-flash"
  "codex:gpt-5.5"
  "claude:claude-opus-4-7"
  "claude:claude-sonnet-4-6"
  "claude:claude-haiku-4-5-20251001"
)

# ---- shared helpers (used by every mode) -----------------------------------

make_run_id() { # TS agent model
  local model_safe
  model_safe="$(printf '%s' "$3" | tr -c 'A-Za-z0-9._-' '_')"
  printf '%s__%s_%s' "$1" "$2" "$model_safe"
}

make_log() { # LOG_DIR agent model
  local model_safe
  model_safe="$(printf '%s' "$3" | tr -c 'A-Za-z0-9._-' '_')"
  printf '%s/%s_%s.log' "$1" "$2" "$model_safe"
}

cleanup_docker() {
  if [[ "${NO_CLEANUP:-0}" == "1" ]]; then
    echo "==> NO_CLEANUP=1, leaving Docker resources in place."
    return
  fi
  echo
  echo "==> Cleaning up ADE-bench Docker resources..."
  local containers images
  containers="$(docker ps -aq --filter 'name=__client' 2>/dev/null || true)"
  if [[ -n "$containers" ]]; then docker rm -f $containers >/dev/null 2>&1 || true; fi
  images="$(docker images --filter 'reference=ade-bench__*' -q 2>/dev/null | sort -u || true)"
  if [[ -n "$images" ]]; then docker rmi -f $images >/dev/null 2>&1 || true; fi
  docker image prune -f >/dev/null 2>&1 || true
  echo "==> Cleanup done."
}

# ---- re-entrant pane modes -------------------------------------------------
# These run inside the tmux panes. Kept as subcommands (rather than inline tmux
# command strings) to avoid nested-quoting headaches.

case "${1:-}" in
  __model)
    # __model <idx> <TASKS> <DB> <PROJECT_TYPE> <CONCURRENCY> <TS>
    idx="$2"; TASKS="$3"; DB="$4"; PROJECT_TYPE="$5"; CONCURRENCY="$6"; TS="$7"
    entry="${EXPERIMENTS[$idx]}"
    agent="${entry%%:*}"
    model="${entry##*:}"
    run_id="$(make_run_id "$TS" "$agent" "$model")"
    LOG_DIR="experiments/_runlogs/${TS}"
    logfile="$(make_log "$LOG_DIR" "$agent" "$model")"
    done_dir="${LOG_DIR}/_done"
    mkdir -p "$done_dir"

    echo "=================================================================="
    echo " agent=${agent}  model=${model}"
    echo " run-id: ${run_id}"
    echo " log:    ${logfile}"
    echo "=================================================================="
    echo

    set +e
    "$REPO_ROOT/adex" run "$TASKS" \
      --db "$DB" \
      --project-type "$PROJECT_TYPE" \
      --agent "$agent" \
      --model "$model" \
      --run-id "$run_id" \
      --n-concurrent-trials "$CONCURRENCY" 2>&1 | tee "$logfile"
    status="${PIPESTATUS[0]}"
    set -e

    printf '%s' "$status" > "${done_dir}/${idx}"
    echo
    if [[ "$status" == "0" ]]; then
      echo "==> ${agent}/${model}: completed (exit 0)"
    else
      echo "==> ${agent}/${model}: FAILED (exit ${status})"
    fi
    echo "    Pane stays open for review. Close it with Ctrl-b x, or type 'exit'."
    exec "${SHELL:-/bin/bash}"
    ;;

  __monitor)
    # __monitor <TS> <NO_CLEANUP>
    TS="$2"; NO_CLEANUP="$3"
    LOG_DIR="experiments/_runlogs/${TS}"
    done_dir="${LOG_DIR}/_done"
    n="${#EXPERIMENTS[@]}"

    echo "Monitor: waiting for ${n} model run(s) to finish..."
    while true; do
      count="$(ls "$done_dir" 2>/dev/null | wc -l | tr -d ' ')"
      printf '\r  finished: %s/%s   ' "$count" "$n"
      if [[ "$count" -ge "$n" ]]; then break; fi
      sleep 5
    done
    echo
    echo
    echo "============================ SUMMARY ============================"
    printf "%-52s %s\n" "RUN" "RESULT"
    for entry in "${EXPERIMENTS[@]}"; do
      agent="${entry%%:*}"
      model="${entry##*:}"
      run_id="$(make_run_id "$TS" "$agent" "$model")"
      tsv="experiments/${run_id}/results.tsv"
      if [[ -f "$tsv" ]]; then
        total=$(($(wc -l < "$tsv") - 1))
        passed=$(awk -F'\t' 'NR>1 && $3=="pass"' "$tsv" | wc -l | tr -d ' ')
        printf "%-52s %s\n" "$run_id" "${passed}/${total} passed"
      else
        printf "%-52s %s\n" "$run_id" "no results (run failed early)"
      fi
    done
    echo "Console logs: ${LOG_DIR}"
    echo "================================================================"

    cleanup_docker
    echo
    echo "Monitor done. Pane stays open; close with Ctrl-b x, or type 'exit'."
    exec "${SHELL:-/bin/bash}"
    ;;
esac

# ---- orchestrator (normal invocation) --------------------------------------

TASKS="${1:-all}"
DB="${DB:-duckdb}"
PROJECT_TYPE="${PROJECT_TYPE:-dbt}"
CONCURRENCY="${CONCURRENCY:-4}"
NO_CLEANUP="${NO_CLEANUP:-0}"
SESSION="${TMUX_SESSION:-ade-experiments}"
NO_ATTACH="${NO_ATTACH:-0}"

if ! command -v tmux >/dev/null 2>&1; then
  echo "error: tmux is required but not installed." >&2
  exit 1
fi

TS="$(date +%Y-%m-%d__%H-%M-%S)"
LOG_DIR="experiments/_runlogs/${TS}"
mkdir -p "${LOG_DIR}/_done"

echo "Experiment matrix: ${#EXPERIMENTS[@]} models | tasks=${TASKS} db=${DB} project=${PROJECT_TYPE} concurrency=${CONCURRENCY}"
echo "tmux session: ${SESSION}"

# Build the command a pane should run for model <idx>. Single-quote each value;
# none of them contain single quotes.
pane_cmd() {
  printf "exec '%s' __model '%s' '%s' '%s' '%s' '%s' '%s'" \
    "$SELF" "$1" "$TASKS" "$DB" "$PROJECT_TYPE" "$CONCURRENCY" "$TS"
}
monitor_cmd() {
  printf "exec '%s' __monitor '%s' '%s'" "$SELF" "$TS" "$NO_CLEANUP"
}

# Fresh session.
tmux kill-session -t "$SESSION" 2>/dev/null || true

# Pane per model.
pid="$(tmux new-session -d -s "$SESSION" -n models -P -F '#{pane_id}' "$(pane_cmd 0)")"
tmux select-pane -t "$pid" -T "${EXPERIMENTS[0]}"
for (( idx = 1; idx < ${#EXPERIMENTS[@]}; idx++ )); do
  pid="$(tmux split-window -t "$SESSION" -P -F '#{pane_id}' "$(pane_cmd "$idx")")"
  tmux select-pane -t "$pid" -T "${EXPERIMENTS[$idx]}"
  tmux select-layout -t "$SESSION" tiled >/dev/null
done

# Monitor pane (summary + cleanup once everything finishes).
pid="$(tmux split-window -t "$SESSION" -P -F '#{pane_id}' "$(monitor_cmd)")"
tmux select-pane -t "$pid" -T "monitor"

tmux select-layout -t "$SESSION" tiled >/dev/null
tmux setw -t "$SESSION" pane-border-status top >/dev/null 2>&1 || true
tmux setw -t "$SESSION" pane-border-format ' #{pane_title} ' >/dev/null 2>&1 || true

if [[ "$NO_ATTACH" == "1" ]]; then
  echo "Session ready (NO_ATTACH=1). Attach with: tmux attach -t ${SESSION}"
elif [[ -n "${TMUX:-}" ]]; then
  # Already inside tmux: can't nest an attach, switch to the new session.
  tmux switch-client -t "$SESSION" 2>/dev/null || \
    echo "Session ready. Switch with: tmux switch-client -t ${SESSION}"
else
  tmux attach -t "$SESSION"
fi
