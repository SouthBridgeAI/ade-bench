import os
import shlex
from pathlib import Path
from typing import Any

from ade_bench.agents.agent_name import AgentName
from ade_bench.agents.base_agent import AgentResult
from ade_bench.agents.installed_agents.abstract_installed_agent import (
    AbstractInstalledAgent,
)
from ade_bench.agents.installed_agents.claude_code.log_formatter import ClaudeCodeLogFormatter
from ade_bench.harness_models import TerminalCommand
from ade_bench.parsers.claude_parser import ClaudeParser
from ade_bench.terminal.tmux_session import TmuxSession
from ade_bench.utils.logger import log_harness_info, logger
from ade_bench.config import config


_PROJECT_ROOT_CREDENTIALS = Path(__file__).resolve().parents[4] / ".claude-credentials.json"
_CONTAINER_CREDENTIALS_DIR = "/root/.claude"
_CONTAINER_CREDENTIALS_FILENAME = ".credentials.json"


class ClaudeCodeAgent(AbstractInstalledAgent):
    NAME = AgentName.CLAUDE_CODE

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._claude_parser = ClaudeParser()
        self._log_formatter = ClaudeCodeLogFormatter()

    @property
    def _env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
        return env

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
        if not _PROJECT_ROOT_CREDENTIALS.exists():
            return
        log_harness_info(
            logger,
            task_name,
            "agent",
            f"Found {_PROJECT_ROOT_CREDENTIALS.name}; mounting Claude Code OAuth credentials into container",
        )
        session.container.exec_run(
            ["sh", "-c", f"mkdir -p {_CONTAINER_CREDENTIALS_DIR} && chmod 700 {_CONTAINER_CREDENTIALS_DIR}"]
        )
        session.copy_to_container(
            _PROJECT_ROOT_CREDENTIALS,
            container_dir=_CONTAINER_CREDENTIALS_DIR,
            container_filename=_CONTAINER_CREDENTIALS_FILENAME,
        )
        session.container.exec_run(
            ["sh", "-c", f"chmod 600 {_CONTAINER_CREDENTIALS_DIR}/{_CONTAINER_CREDENTIALS_FILENAME}"]
        )

    @property
    def _install_agent_script(self) -> os.PathLike:
        return Path(__file__).parent / "claude-code-setup.sh"

    def _run_agent_commands(self, task_prompt: str) -> list[TerminalCommand]:
        header = "echo 'AGENT RESPONSE: ' && "
        escaped_prompt = shlex.quote(task_prompt)
        command = f"{header} claude --output-format stream-json --verbose -p {escaped_prompt}"

        if self._model_name:
            command += f" --model {self._model_name}"

        if self._allowed_tools:
            command += f" --allowedTools {' '.join(self._allowed_tools)}"

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
        """Parse Claude agent output to extract metrics."""
        # The output should now be cleaner since we're using capture_entire=False
        # But let's still try to extract just the JSON part if there's any extra content
        return self._claude_parser.parse(output)

    def format_agent_log(self, log_path: Path) -> str | None:
        """
        Format the Claude Code agent's log file into a human-readable string.

        Also generates an HTML transcript at log_path.parent / "transcript.html"
        using claude-code-transcripts if available.


        Args:
            log_path: Path to the raw agent.log file (JSON-lines format)

        Returns:
            Formatted log content as a string, or None if formatting failed
        """
        # Generate HTML transcript as a single well-known file
        transcript_path = log_path.parent / "transcript.html"
        self._log_formatter.generate_html_transcript(log_path, transcript_path)

        # Return text-formatted log
        return self._log_formatter.format_log(log_path)

    # Generic tools to filter out from tools_used reporting
    _GENERIC_TOOLS = frozenset(
        {
            "Bash",
            "Edit",
            "Glob",
            "Grep",
            "Read",
            "Write",
            "WebFetch",
            "WebSearch",
            "Task",
            "NotebookEdit",
            "TodoRead",
            "TodoWrite",
        }
    )

    def extract_tools_used(self, log_path: Path) -> list[str] | None:
        """
        Extract deduplicated tool names from Claude Code agent logs.

        Filters out generic tools (Bash, Edit, Glob, etc.) and expands
        Skill tool calls to their actual skill names.
        """
        try:
            turns = self._log_formatter.parse_log_file(log_path)
            tool_names = set()
            for turn in turns:
                for tool in turn.get("tools", []):
                    name = tool["name"]
                    # Expand Skill tool to actual skill name
                    if name == "Skill":
                        skill_name = tool.get("input", {}).get("skill")
                        if skill_name:
                            tool_names.add(f"skill:{skill_name}")
                    # Filter out generic tools
                    elif name not in self._GENERIC_TOOLS:
                        tool_names.add(name)
            return sorted(tool_names) if tool_names else None
        except Exception:
            return None
