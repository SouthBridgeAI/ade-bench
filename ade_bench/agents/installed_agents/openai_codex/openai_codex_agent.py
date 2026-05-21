import os
import shlex
from pathlib import Path
from typing import Any

from ade_bench.agents.agent_name import AgentName
from ade_bench.agents.base_agent import AgentResult
from ade_bench.agents.installed_agents.abstract_installed_agent import (
    AbstractInstalledAgent,
)
from ade_bench.harness_models import TerminalCommand
from ade_bench.parsers.codex_parser import CodexParser
from ade_bench.terminal.tmux_session import TmuxSession
from ade_bench.utils.logger import log_harness_info, logger
from ade_bench.config import config


_PROJECT_ROOT_CODEX_AUTH = Path(__file__).resolve().parents[4] / ".codex-auth.json"
_CONTAINER_CODEX_DIR = "/root/.codex"
_CONTAINER_CODEX_AUTH_FILENAME = "auth.json"


class OpenAICodexAgent(AbstractInstalledAgent):
    NAME = AgentName.OPENAI_CODEX
    # Codex doesn't seem to have an allowed tools option, but I didn't fully check.
    # ALLOWED_TOOLS = ["Bash", "Edit", "Write", "NotebookEdit", "WebFetch"]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._codex_parser = CodexParser()

    @property
    def _use_subscription(self) -> bool:
        return _PROJECT_ROOT_CODEX_AUTH.exists()

    @property
    def _env(self) -> dict[str, str]:
        # In subscription mode the OAuth credentials are mounted into the
        # container; injecting OPENAI_API_KEY would make Codex prefer api-key auth.
        if self._use_subscription:
            return {}
        return {
            "OPENAI_API_KEY": os.environ["OPENAI_API_KEY"],
        }

    @property
    def _install_agent_script(self) -> os.PathLike:
        return Path(__file__).parent / "openai_codex-setup.sh"

    def perform_task(
        self,
        task_prompt: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
        task_name: str | None = None,
    ) -> AgentResult:
        self._copy_credentials_into_container(session, task_name)
        return super().perform_task(
            task_prompt=task_prompt,
            session=session,
            logging_dir=logging_dir,
            task_name=task_name,
        )

    def _copy_credentials_into_container(
        self, session: TmuxSession, task_name: str | None
    ) -> None:
        if not _PROJECT_ROOT_CODEX_AUTH.exists():
            return
        log_harness_info(
            logger,
            task_name,
            "agent",
            f"Found {_PROJECT_ROOT_CODEX_AUTH.name}; mounting Codex OAuth credentials into container",
        )
        session.container.exec_run(
            ["sh", "-c", f"mkdir -p {_CONTAINER_CODEX_DIR} && chmod 700 {_CONTAINER_CODEX_DIR}"]
        )
        session.copy_to_container(
            _PROJECT_ROOT_CODEX_AUTH,
            container_dir=_CONTAINER_CODEX_DIR,
            container_filename=_CONTAINER_CODEX_AUTH_FILENAME,
        )
        session.container.exec_run(
            ["sh", "-c", f"chmod 600 {_CONTAINER_CODEX_DIR}/{_CONTAINER_CODEX_AUTH_FILENAME}"]
        )

    def _run_agent_commands(self, task_prompt: str) -> list[TerminalCommand]:
        escaped_prompt = shlex.quote(task_prompt)

        if self._model_name:
            model_command = f" --model {self._model_name}"
        else:
            model_command = ""

        # In subscription mode Codex reads the mounted ~/.codex/auth.json; in
        # api-key mode we log in with the OPENAI_API_KEY first.
        login_prefix = "" if self._use_subscription else "printenv OPENAI_API_KEY | codex login --with-api-key && "

        command = (
            f"echo 'AGENT RESPONSE: ' && "
            f"{login_prefix}"
            f"codex --ask-for-approval never {model_command} "
            f"exec "
            f"--json --sandbox danger-full-access --skip-git-repo-check "
            f"{escaped_prompt}"
        )

        return [
            TerminalCommand(
                command=command,
                min_timeout_sec=0.0,
                max_timeout_sec=config.default_agent_timeout_sec,
                block=True,
                append_enter=True,
            )
        ]

    def _parse_agent_output(self, output: str) -> dict[str, Any]:
        """Parse Codex agent output to extract metrics."""
        return self._codex_parser.parse(output)
