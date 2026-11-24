"""The Codex boundary never launches a real process in tests."""

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import BaseModel

from jobagent.ai_codex import run_codex


class Answer(BaseModel):
    answer: str


def test_locked_down_argv_environment_cwd_and_validated_output(monkeypatch, caplog):
    for key in ("TELEGRAM_BOT_TOKEN", "NOTION_API_KEY", "WORKDAY_PASSWORD"):
        monkeypatch.setenv(key, "SECRET123")
    monkeypatch.setenv("PATH", "/bin")
    monkeypatch.setenv("JOBAGENT_CODEX_BIN", "test-codex")
    monkeypatch.delenv("JOBAGENT_INTENT_CODEX_MODEL", raising=False)
    directories = []

    def run(argv, **kwargs):
        cwd = kwargs["cwd"]
        directories.append(cwd)
        assert cwd.name.startswith("jobagent-codex-") and (cwd / ".git").is_dir()
        assert argv == [
            "test-codex",
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--ignore-user-config",
            "--ignore-rules",
            "--disable",
            "shell_tool",
            "--disable",
            "unified_exec",
            "--disable",
            "browser_use",
            "--disable",
            "computer_use",
            "--disable",
            "in_app_browser",
            "--disable",
            "apps",
            "--disable",
            "plugins",
            "--disable",
            "hooks",
            "--disable",
            "image_generation",
            "-c",
            'web_search="disabled"',
            "-c",
            'shell_environment_policy.inherit="none"',
            "--output-schema",
            str(cwd / "schema.json"),
            "-o",
            str(cwd / "output.json"),
            "-",
        ]
        assert not any(arg.startswith("--dangerously") for arg in argv)
        assert set(kwargs["env"]) <= {"PATH", "HOME", "LANG", "CODEX_HOME"}
        assert kwargs["env"]["PATH"] == "/bin"
        assert kwargs | {"env": None, "cwd": None} == {
            "cwd": None,
            "env": None,
            "input": "PRIVATE_PROMPT",
            "capture_output": True,
            "text": True,
            "timeout": 35,
            "check": False,
        }
        assert json.loads((cwd / "schema.json").read_text())["required"] == ["answer"]
        (cwd / "output.json").write_text('{"answer":"PRIVATE_REPLY"}')
        return subprocess.CompletedProcess(argv, 0, "SECRET123", "SECRET123")

    monkeypatch.setattr("jobagent.ai_codex.subprocess.run", run)
    with caplog.at_level("INFO"):
        result = run_codex("PRIVATE_PROMPT", Answer, "intent", timeout=35)
    assert result == Answer(answer="PRIVATE_REPLY")
    assert not directories[0].exists()
    assert "SECRET123" not in caplog.text and "PRIVATE" not in caplog.text


def test_optional_model(monkeypatch):
    monkeypatch.setenv("JOBAGENT_INTENT_CODEX_MODEL", "router-model")

    def run(argv, **kwargs):
        assert argv[argv.index("--model") + 1] == "router-model"
        Path(argv[argv.index("-o") + 1]).write_text('{"answer":"ok"}')
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("jobagent.ai_codex.subprocess.run", run)
    assert run_codex("message", Answer, "intent", timeout=1).answer == "ok"


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (FileNotFoundError("SECRET123"), "Codex executable not found"),
        (subprocess.TimeoutExpired("SECRET123", 1, stderr="SECRET123"), "Codex intent timed out"),
        (
            subprocess.CompletedProcess([], 7, "SECRET123", "SECRET123"),
            "Codex intent failed (exit 7)",
        ),
        (subprocess.CompletedProcess([], 0), "Codex returned an invalid intent"),
    ],
)
def test_safe_errors(monkeypatch, failure, message):
    mocked = (
        Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
    )
    monkeypatch.setattr("jobagent.ai_codex.subprocess.run", mocked)
    with pytest.raises(RuntimeError) as caught:
        run_codex("private", Answer, "intent", timeout=1)
    assert str(caught.value) == message
    assert "SECRET123" not in str(caught.value)


@pytest.mark.parametrize("output", ["not json", "{}", '{"answer":123}'])
def test_invalid_output(monkeypatch, output):
    def run(argv, **kwargs):
        Path(argv[argv.index("-o") + 1]).write_text(output)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("jobagent.ai_codex.subprocess.run", run)
    with pytest.raises(RuntimeError, match="Codex returned an invalid intent"):
        run_codex("private", Answer, "intent", timeout=1)
