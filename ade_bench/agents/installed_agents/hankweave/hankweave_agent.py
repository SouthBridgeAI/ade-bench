"""
ade-bench agent that solves a task by running a one-codon Hankweave hank.

Flow per task (all inside the sandbox container, cwd /app = the dbt project ade grades):
  1. `hankweave-setup.sh` (copied + sourced once) writes the hank + helper scripts.
  2. The task prompt is written to /tmp/hw_task_prompt.txt (base64-decoded, quoting-safe).
  3. `run-hankweave.sh` snapshots /app into a read-only data source, runs
     `hankweave hank.json <data> --model haiku`, then mirrors the agent's edited copy
     (agentRoot/project) back into /app so ade can score it with dbt tests.
  4. The runner emits a final `{...input_tokens...}` JSON line that the base class parses.

Auth: hankweave's embedded Claude Agent SDK authenticates from env vars (its startup self-test
requires ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN; it does NOT read the OAuth credentials
file). We forward the subscription OAuth token from .claude-credentials.json when present (works
even when the workspace API key is usage-capped), otherwise ANTHROPIC_API_KEY.
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

PROMPT_FILE = "/tmp/hw_task_prompt.txt"

# Same depth as the claude agent: parents[4] == the ade-bench (submodule) root, where the
# superbench adapter symlinks the project-root .claude-credentials.json for subscription auth.
_PROJECT_ROOT_CREDENTIALS = Path(__file__).resolve().parents[4] / ".claude-credentials.json"


class HankweaveAgent(AbstractInstalledAgent):
    NAME = AgentName.HANKWEAVE

    @property
    def _env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        # Prefer the subscription OAuth token (passes hankweave's self-test and isn't subject to
        # the workspace API usage cap); fall back to ANTHROPIC_API_KEY when no creds exist.
        token = self._oauth_access_token()
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        else:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if api_key:
                env["ANTHROPIC_API_KEY"] = api_key
        # The ade sandbox runs as root, where Claude Code refuses --dangerously-skip-permissions
        # (which hankweave uses to run the agent headlessly) unless IS_SANDBOX=1 is set.
        env["IS_SANDBOX"] = "1"
        env["HANKWEAVE_MODEL"] = self._model_name or "haiku"
        return env

    @staticmethod
    def _oauth_access_token() -> str | None:
        """Read the subscription OAuth access token from the (symlinked) credentials file."""
        try:
            data = json.loads(_PROJECT_ROOT_CREDENTIALS.read_text())
        except Exception:
            return None
        token = (data or {}).get("claudeAiOauth", {}).get("accessToken")
        return token if isinstance(token, str) and token else None

    @property
    def _install_agent_script(self) -> os.PathLike:
        return Path(__file__).parent / "hankweave-setup.sh"

    def _run_agent_commands(self, task_prompt: str) -> list[TerminalCommand]:
        # Write the prompt as base64: ade wraps run commands with `2>&1 | tee` (no heredocs) and
        # exports env via `export K='V'`, so single-quote-fragile payloads must be base64-encoded.
        b64 = base64.b64encode(task_prompt.encode("utf-8")).decode("ascii")
        write_prompt = TerminalCommand(
            command=f"echo {b64} | base64 -d > {PROMPT_FILE}",
            min_timeout_sec=0.0,
            max_timeout_sec=30.0,
            block=True,
            append_enter=True,
        )
        run = TerminalCommand(
            command="bash /installed-agent/run-hankweave.sh",
            min_timeout_sec=0.0,
            max_timeout_sec=config.default_agent_timeout_sec,
            block=True,
            append_enter=True,
        )
        return [write_prompt, run]

    def _parse_agent_output(self, output: str) -> dict[str, Any]:
        """run-hankweave.sh emits the final metrics JSON line; parse the last matching one."""
        metrics: dict[str, Any] = {
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
