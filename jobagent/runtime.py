"""Local setup, doctor checks, process lock, and safe launchd rendering."""
from __future__ import annotations

import os
import plistlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from jobagent.apply.profile import ApplicationConfigurationError, load_application_profile
from jobagent.candidate import load_candidate_truth


@dataclass(frozen=True)
class Check: name: str; state: str; detail: str

def _chromium_check() -> Check:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return Check("Playwright/Chromium", "WARN", "playwright is not installed")
    try:
        with sync_playwright() as playwright:
            path = playwright.chromium.executable_path
        if path and os.path.exists(path):
            return Check("Playwright/Chromium", "PASS", path)
        return Check("Playwright/Chromium", "WARN", "not installed; run `uv run playwright install chromium`")
    except Exception as error:  # noqa: BLE001 - any driver failure is just a WARN here
        return Check("Playwright/Chromium", "WARN", str(error)[:200])

def doctor(database: Path, *, offline: bool = True) -> list[Check]:
    checks = [Check("Python/runtime", "PASS", "Python runtime available"), Check("Database", "PASS", str(database))]
    try: load_candidate_truth(); checks.append(Check("Candidate truth", "PASS", "valid"))
    except ValueError as error: checks.append(Check("Candidate truth", "FAIL", str(error)[:200]))
    try: load_application_profile(); checks.append(Check("Application profile", "PASS", "valid"))
    except ApplicationConfigurationError as error: checks.append(Check("Application profile", "FAIL", str(error)[:200]))
    master_docx = Path("candidate/master_resume.docx")
    checks.append(Check("Master resume", "PASS" if master_docx.is_file() else "FAIL", str(master_docx) if master_docx.is_file() else "candidate/master_resume.docx (the Word source) is required for resume tailoring"))
    template = Path("candidate/cover_letter_template.docx")
    checks.append(Check("Cover letter template", "PASS" if template.is_file() else "WARN", str(template) if template.is_file() else "candidate/cover_letter_template.docx not found; cover letters are skipped"))
    claude = shutil.which(os.getenv("JOBAGENT_CLAUDE_BIN", "claude"))
    checks.append(Check("Claude Code CLI", "PASS" if claude else "WARN", "writes cover letters" if claude else "not found; cover letters are skipped"))
    checks.append(_pdf_export_check())
    notion = os.getenv("NOTION_API_KEY") and os.getenv("NOTION_DATABASE_ID")
    checks += [Check("Codex CLI", "PASS" if shutil.which(os.getenv("JOBAGENT_CODEX_BIN", "codex")) else "WARN", "found" if shutil.which(os.getenv("JOBAGENT_CODEX_BIN", "codex")) else "not found; authenticate with `codex`"), Check("PDF renderer", "PASS", "reportlab + pypdf"), _chromium_check(), Check("Notion", "PASS" if notion else "WARN", "configured" if notion else "set NOTION_API_KEY and NOTION_DATABASE_ID, then `jobagent notion setup`"), Check("Telegram", "PASS" if os.getenv("TELEGRAM_BOT_TOKEN") else "WARN", "configured" if os.getenv("TELEGRAM_BOT_TOKEN") else "not configured")]
    return checks

def _pdf_export_check() -> Check:
    """Whichever exporter `default_pdf_converter` will use here: Word on the Mac, LibreOffice on the VPS."""
    from jobagent.writing.resume import default_pdf_converter, libreoffice_to_pdf

    if default_pdf_converter() is libreoffice_to_pdf:
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        return Check("LibreOffice", "PASS" if soffice else "FAIL", "exports resume PDFs" if soffice else "install libreoffice to export resume PDFs")
    word = Path("/Applications/Microsoft Word.app").exists()
    return Check("Microsoft Word", "PASS" if word else "FAIL", "used to export resume PDFs" if word else "required to export resume PDFs")

class WorkflowLock:
    """A local exclusive lock that self-heals if its owning process has died."""

    def __init__(self, path: Path = Path("data/jobagent.lock")) -> None:
        self.path = path

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if not self._owner_is_running():
                    self.path.unlink(missing_ok=True)
                    continue
                raise RuntimeError("Another JobAgent workflow is already running.") from None
            else:
                os.write(self.fd, str(os.getpid()).encode())
                return self
        raise RuntimeError("Another JobAgent workflow is already running.")

    def _owner_is_running(self) -> bool:
        """Return whether the process that created the lock is still alive."""
        try:
            pid = int(self.path.read_text().strip())
        except (OSError, ValueError):
            return False  # unreadable or corrupt lock file; treat as stale
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # process exists but is owned by someone else
        return True

    def __exit__(self, *args: object) -> None:
        os.close(self.fd)
        self.path.unlink(missing_ok=True)

def launchd_plist(repo: Path, uv: Path, time: str, command: list[str], label: str) -> str:
    hour, minute = time.split(":")
    args = "".join(f"<string>{item}</string>" for item in [str(uv), "run", "jobagent", *command])
    return f'<?xml version="1.0"?><plist version="1.0"><dict><key>Label</key><string>{label}</string><key>ProgramArguments</key><array>{args}</array><key>WorkingDirectory</key><string>{repo}</string><key>StartCalendarInterval</key><dict><key>Hour</key><integer>{int(hour)}</integer><key>Minute</key><integer>{int(minute)}</integer></dict></dict></plist>'


def bot_launchd_plist(repo: Path, uv: Path, path_env: str, label: str = "com.jobagent.bot") -> str:
    """Always-on LaunchAgent for `jobagent bot`; secrets stay in the repo's .env."""
    return plistlib.dumps({
        "Label": label,
        "ProgramArguments": [str(uv), "run", "jobagent", "bot"],
        "WorkingDirectory": str(repo),
        "EnvironmentVariables": {"PATH": path_env},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "AbandonProcessGroup": True,  # a bot restart must not kill a running /fill or /run child
        "StandardOutPath": str(repo / "logs" / "bot.out.log"),
        "StandardErrorPath": str(repo / "logs" / "bot.err.log"),
    }).decode()
