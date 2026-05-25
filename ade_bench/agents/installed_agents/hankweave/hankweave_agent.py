"""
ade-bench agent that solves a task by running a one-codon Hankweave hank.

Flow per task (all inside the sandbox container, cwd /app = the dbt project ade grades):
  1. The hank (hank.json + prompt-header.md) is synced from the project-root
     `hanks/ade-bench/<hank>` (hank name from $HANKWEAVE_HANK, default "base"; set via the bench
     `--hank` flag) into /installed-agent/hank, and the run-hankweave.sh + hw-metrics.js glue is
     copied to /installed-agent (see perform_task). `hankweave-setup.sh` is then sourced as a thin
     installer (PATH + bun sanity check); it no longer generates any files.
  2. The task prompt is written to /tmp/hw_task_prompt.txt (base64-decoded, quoting-safe).
  3. `run-hankweave.sh` snapshots /app into a read-only data source, composes the codon prompt
     (prompt-header.md + task prompt), runs `hankweave hank.json <data> --model haiku`, then mirrors
     the agent's edited copy (agentRoot/project) back into /app so ade can score it with dbt tests.
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
from ade_bench.agents.base_agent import AgentResult
from ade_bench.agents.installed_agents.abstract_installed_agent import (
    AbstractInstalledAgent,
)
from ade_bench.config import config
from ade_bench.harness_models import TerminalCommand
from ade_bench.terminal.tmux_session import TmuxSession
from ade_bench.utils.logger import log_harness_info, logger

PROMPT_FILE = "/tmp/hw_task_prompt.txt"

# Same depth as the claude agent: parents[4] == the ade-bench (submodule) root, where the
# superbench adapter symlinks the project-root .claude-credentials.json for subscription auth.
_PROJECT_ROOT_CREDENTIALS = Path(__file__).resolve().parents[4] / ".claude-credentials.json"

# Hanks live at the superbench project root (parents[6] == one level above the ade-bench submodule),
# scoped per benchmark: hanks/<benchmark>/<hank-name>/. This agent serves ade-bench, so it resolves
# hanks/ade-bench/<HANKWEAVE_HANK or "base">. The selected hank is synced into the container by
# perform_task; the ade-integration glue (run-hankweave.sh, hw-metrics.js) lives next to this file
# and is copied in alongside it.
_PROJECT_ROOT_HANKS = Path(__file__).resolve().parents[6] / "hanks"
_BENCHMARK_DIR = "ade-bench"
_DEFAULT_HANK = "base"
_AGENT_DIR = Path(__file__).resolve().parent
_CONTAINER_HANK_DIR = "/installed-agent/hank"


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

    def perform_task(
        self,
        task_prompt: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
        task_name: str | None = None,
    ) -> AgentResult:
        # Sync the hank + glue into the container before the base class installs the thin setup
        # script and runs the agent commands (mirrors claude_code/openai_codex credential copies).
        self._copy_hank_into_container(session, task_name)
        return super().perform_task(
            task_prompt=task_prompt,
            session=session,
            logging_dir=logging_dir,
            task_name=task_name,
        )

    def _copy_hank_into_container(self, session: TmuxSession, task_name: str | None) -> None:
        """Sync the selected hanks/ade-bench/<hank> hank + the ade glue scripts into the container."""
        hank_name = os.environ.get("HANKWEAVE_HANK") or _DEFAULT_HANK
        hank_dir = _PROJECT_ROOT_HANKS / _BENCHMARK_DIR / hank_name
        if not hank_dir.is_dir():
            raise FileNotFoundError(
                f"Hankweave hank not found at {hank_dir}. Expected a "
                f"'hanks/{_BENCHMARK_DIR}/{hank_name}' directory (hank.json + prompt-header.md) at "
                "the superbench project root (set via --hank; default 'base')."
            )
        log_harness_info(
            logger,
            task_name,
            "agent",
            f"Syncing hank '{hank_name}' from {hank_dir} -> {_CONTAINER_HANK_DIR}",
        )
        # put_archive requires the target dir to exist; this also creates /installed-agent so the
        # glue copy below lands correctly even before the base class copies the setup script.
        session.container.exec_run(["sh", "-c", f"mkdir -p {_CONTAINER_HANK_DIR}"])
        session.copy_to_container(
            hank_dir,
            container_dir=_CONTAINER_HANK_DIR,
        )
        session.copy_to_container(
            [_AGENT_DIR / "run-hankweave.sh", _AGENT_DIR / "hw-metrics.js"],
            container_dir="/installed-agent",
        )

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
