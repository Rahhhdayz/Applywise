"""Schema-only Codex calls with tools disabled and a minimal child environment."""

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, ValidationError

from jobagent.scoring import _codex_output_schema

log = logging.getLogger(__name__)


def run_codex[T: BaseModel](prompt: str, schema: type[T], what: str, *, timeout: float) -> T:
    with tempfile.TemporaryDirectory(prefix="jobagent-codex-") as directory:
        cwd = Path(directory)
        (cwd / ".git").mkdir()
        schema_path, output = cwd / "schema.json", cwd / "output.json"
        schema_path.write_text(json.dumps(_codex_output_schema(schema.model_json_schema())))
        argv = [
            os.getenv("JOBAGENT_CODEX_BIN", "codex"),
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--ignore-user-config",
            "--ignore-rules",
        ]
        for feature in (
            "shell_tool",
            "unified_exec",
            "browser_use",
            "computer_use",
            "in_app_browser",
            "apps",
            "plugins",
            "hooks",
            "image_generation",
        ):
            argv.extend(["--disable", feature])
        argv.extend(
            ["-c", 'web_search="disabled"', "-c", 'shell_environment_policy.inherit="none"']
        )
        if model := os.getenv("JOBAGENT_INTENT_CODEX_MODEL", ""):
            argv.extend(["--model", model])
        argv.extend(["--output-schema", str(schema_path), "-o", str(output), "-"])
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "CODEX_HOME")
            if key in os.environ
        }
        try:
            result = subprocess.run(
                argv,
                cwd=cwd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=env,
            )
        except FileNotFoundError:
            raise RuntimeError("Codex executable not found") from None
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Codex {what} timed out") from None
        log.info("Codex exit code: %d", result.returncode)
        if result.returncode:
            raise RuntimeError(f"Codex {what} failed (exit {result.returncode})")
        try:
            return schema.model_validate_json(output.read_text())
        except (OSError, ValueError, ValidationError):
            raise RuntimeError(f"Codex returned an invalid {what}") from None
