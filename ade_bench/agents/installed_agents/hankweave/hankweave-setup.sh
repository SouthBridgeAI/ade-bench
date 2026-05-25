#!/bin/bash
# Installed once per task by ade: copied into the container and `source`d (NOT wrapped with
# `2>&1 | tee`). This is now a thin installer — it sets PATH and sanity-checks bun (baked into the
# base image). It no longer generates any files: the hank (hank.json + prompt-header.md) is synced
# into /installed-agent/hank from the project-root hanks/ade-bench/<hank>, and the run-hankweave.sh +
# hw-metrics.js glue is copied to /installed-agent, both by HankweaveAgent.perform_task.
set -e
echo "Setup hankweave agent"
export PATH="/usr/local/bin:$HOME/.bun/bin:$PATH"

if command -v bun >/dev/null 2>&1; then
  echo "bun: $(bun --version)"
else
  echo "ERROR: bun not found on PATH — rebuild the ade base image with bun baked in" >&2
fi

echo "hankweave agent ready"
