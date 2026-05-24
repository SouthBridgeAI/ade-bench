"""
Generic bridge agent for external "bench" harnesses.

The `bench` CLI (superbench monorepo) drives ade-bench with a custom environment harness
by setting these env vars on the `ade run` process:

  BENCH_HARNESS_INSTALL   - shell command to install the harness in the sandbox (optional)
  BENCH_HARNESS_RUN       - shell command to run the harness (required)
  BENCH_HARNESS_ENV_<K>   - extra env vars exported in the sandbox as <K> (e.g. API keys)

The task prompt is written to $BENCH_TASK_PROMPT_FILE inside the sandbox before the run
command executes. The harness operates on the dbt project (the sandbox cwd) and ade scores
it with dbt tests, exactly like the built-in agents.
"""

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

from ade_bench.agents.agent_name import AgentName
from ade_bench.agents.installed_agents.abstract_installed_agent import (
    AbstractInstalledAgent,
)
from ade_bench.config import config
from ade_bench.harness_models import TerminalCommand

PROMPT_FILE = "/tmp/bench_task_prompt.txt"


class BenchHarnessAgent(AbstractInstalledAgent):
    NAME = AgentName.BENCH_HARNESS

    @property
    def _env(self) -> dict[str, str]:
        # ade's setup-env writer wraps values in single quotes (export K='V'), which breaks
        # if V contains a single quote. Harness commands routinely do (echo '...'), so pass
        # them base64-encoded (quote-safe alphabet) and decode them in the sandbox.
        install = os.environ.get("BENCH_HARNESS_INSTALL", "")
        run = os.environ.get("BENCH_HARNESS_RUN", "")
        env: dict[str, str] = {
            "BENCH_HARNESS_INSTALL_B64": base64.b64encode(install.encode()).decode("ascii"),
            "BENCH_HARNESS_RUN_B64": base64.b64encode(run.encode()).decode("ascii"),
            "BENCH_TASK_PROMPT_FILE": PROMPT_FILE,
        }
        # Forward BENCH_HARNESS_ENV_<K> as <K> into the sandbox (e.g. provider keys).
        for key, value in os.environ.items():
            if key.startswith("BENCH_HARNESS_ENV_"):
                env[key[len("BENCH_HARNESS_ENV_") :]] = value
        if self._model_name:
            env["BENCH_HARNESS_MODEL"] = self._model_name
        return env

    @property
    def _install_agent_script(self) -> os.PathLike:
        return Path(__file__).parent / "bench_harness-setup.sh"

    def _run_agent_commands(self, task_prompt: str) -> list[TerminalCommand]:
        # Write the task prompt to a file as a SINGLE-LINE command. The harness wraps each
        # command with `2>&1 | tee ...`, which would break a heredoc, so base64-decode instead.
        b64 = base64.b64encode(task_prompt.encode("utf-8")).decode("ascii")
        write_prompt = TerminalCommand(
            command=f"echo {b64} | base64 -d > {PROMPT_FILE}",
            min_timeout_sec=0.0,
            max_timeout_sec=30.0,
            block=True,
            append_enter=True,
        )
        # Decode the harness run command to a script, then execute it in the project cwd.
        write_run = TerminalCommand(
            command='echo "$BENCH_HARNESS_RUN_B64" | base64 -d > /tmp/bench_run.sh',
            min_timeout_sec=0.0,
            max_timeout_sec=30.0,
            block=True,
            append_enter=True,
        )
        run = TerminalCommand(
            command="bash /tmp/bench_run.sh",
            min_timeout_sec=0.0,
            max_timeout_sec=config.default_agent_timeout_sec,
            block=True,
            append_enter=True,
        )
        return [write_prompt, write_run, run]

    def _parse_agent_output(self, output: str) -> dict[str, Any]:
        """If the harness emits a JSON metrics line, use it; otherwise zeros."""
        metrics = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_tokens": 0,
            "num_turns": 0,
            "runtime_ms": 0,
            "cost_usd": 0.0,
        }
        for line in reversed(output.splitlines()):
            line = line.strip()
            if not (line.startswith("{") and "input_tokens" in line):
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            for k in metrics:
                if k in data:
                    metrics[k] = data[k]
            break
        if re.search(r"quota|rate limit|usage limit", output, re.IGNORECASE):
            metrics["error"] = "quota_exceeded"
        return metrics
