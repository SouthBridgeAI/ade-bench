#!/bin/bash
echo "Setup bench harness"

# Optional install step supplied by the external harness, base64-encoded to survive ade's
# single-quote env export (a raw command with quotes would break the export).
if [ -n "$BENCH_HARNESS_INSTALL_B64" ]; then
  echo "installing bench harness"
  echo "$BENCH_HARNESS_INSTALL_B64" | base64 -d | bash
fi

echo "bench harness ready"
