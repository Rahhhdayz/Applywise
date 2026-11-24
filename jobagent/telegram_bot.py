"""Telegram bot: run Master's daily JobAgent workflow from his phone.

Security model: only the one chat in TELEGRAM_CHAT_ID is served; everything else is ignored and
logged. Message text is never handed to a shell; subprocesses get fixed argv lists with job IDs
parsed as ints. Job page text is untrusted data: it is only matched against fixed keywords.
The bot never submits applications and never sends email. ``/fill`` runs ``apply fill --unattended`` (or
``apply url --unattended``) as one background child after a ✅ tap; that child stops at the final step.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from jobagent.bot_intent import IntentRouter
from jobagent.db import JobRepository, StoredJob
from jobagent.inbox import (
    ClaudeEmailClassifier,
    Decision,
    InboxResult,
    gmail_message_hint,
    proposal_text,
    propose_inbox,
    reader_from_env,
    record_decisions,
    render_email,
    scan_inbox,
    split_for_telegram,
    sync_notion_statuses,
)
from jobagent.intake import IntakeError, UrlIntakeService
from jobagent.integrations import TelegramNotifier, notion_tracker
from jobagent.lifecycle import APPLICATION_STATUSES
from jobagent.pipeline import sync_to_notion

log = logging.getLogger(__name__)

LIMIT = 4096
EMPTY_REPLY = "Nothing found for that. Try /help for the commands."
NO_IMAGES = "I can't read images yet — send the link or describe what you need."
OFFSET_FILE = Path("data/telegram_offset")
CONTEXT_FILE = Path("data/bot_context.json")  # last job + short chat history, so a restart keeps them
LOCK_FILE = Path("data/jobagent.lock")  # WorkflowLock, held by `run cycle`
HELP = """JobAgent bot commands
<job URL> — import it, prepare the packet, sync Notion
/packet <id> — send the resume and cover-letter PDFs
/prepare <id> — build the packet (tailored resume + cover letter), then send it
/applied <id> — mark applied (and sync Notion)
/status <id> <state> — set a lifecycle status
/run — start the daily cycle (one at a time)
/progress — cycle log tail and whether it is running
/inbox — check Gmail for employer replies (read-only); asks before changing any status
/email <id> — the full text of one recorded employer email (plain text, read-only)
/queue [yesterday|YYYY-MM-DD] — that day's cycle counts and queue (default today)
/fill <id or job URL> — fill the application on the Mac and save a draft; sends the link (never submits)
/fillstatus — the running fill and its log tail
/cancel — stop the running fill (closes its browser; nothing is submitted)
/help — this list
Or ask: "any SOC analyst roles I applied to?", "which SOC roles did I apply to last week",
"how many Workday apps are drafted", or "jobs scoring 80+ in Boston"."""

_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_FLAGS = {  # untrusted text is only matched, never interpreted
    "clearance": re.compile(r"\b(security clearance|clearance required|active clearance|ts/sci|top secret|secret clearance|polygraph)\b", re.IGNORECASE),
    "citizenship": re.compile(r"\b(u\.?s\.? citizens?(hip)?|citizenship (is )?required|must be a citizen)\b", re.IGNORECASE),
    "sponsorship mentioned": re.compile(r"\b(sponsorship|sponsor|visa)\b", re.IGNORECASE),
}
_REMOTE = re.compile(r"\bremote\b", re.IGNORECASE)
_BOSTON = re.compile(r"\b(boston|cambridge|somerville|massachusetts|ma)\b", re.IGNORECASE)
_SENIOR = re.compile(r"\b(senior|sr\.?|staff|principal|lead|manager|director|head of)\b", re.IGNORECASE)
_YEARS = re.compile(r"\b(\d{1,2})\s*\+?\s*(?:-|to)?\s*(?:\d{1,2}\s*)?years?\b", re.IGNORECASE)


def trim(text: str, limit: int = LIMIT) -> str:
    """Fit one Telegram message (4096 characters)."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_message(text: str) -> tuple[str, list[str]]:
    """Return (command, args). A plain message holding an http(s) link is ("url", [link])."""
    text = (text or "").strip()
    if text.startswith("/"):
        head, *args = text.split()
        return head[1:].split("@", 1)[0].lower(), args
    match = _URL.search(text)
    if match and valid_url(match.group(0)):
        return "url", [match.group(0)]
    return "unknown", []


def valid_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in {"http", "https"} and bool(parts.netloc) and len(url) <= 2048


def job_flags(job: StoredJob) -> list[str]:
    """Simple keyword checks on the posting; a hint for Master, never a decision."""
    text = job.description_text or ""
    flags = [name for name, pattern in _FLAGS.items() if pattern.search(text)]
    where = f"{job.location or ''} {job.title}"
    if _REMOTE.search(where):
        flags.append("remote")
    elif not _BOSTON.search(job.location or ""):
        flags.append(f"onsite outside Boston ({job.location or 'location unspecified'})")
    if _SENIOR.search(job.title):
        flags.append("senior title")
    years = [int(value) for value in _YEARS.findall(text) if 0 < int(value) <= 30]
    if years:
        flags.append(f"{max(years)}+ years mentioned")
    return flags


def describe(job: StoredJob) -> str:
    flags = job_flags(job)
    return (f"Job #{job.id}: {job.company} / {job.title}\nLocation: {job.location or 'Unspecified'}\n"
            f"Flags: {', '.join(flags) if flags else 'none'}")


def cycle_summary(repository: JobRepository, queue_date: str) -> str:
    """The counts Master asks about for one day's cycle, from workflow_runs (times shown local)."""
    run = repository.cycle_run(queue_date)
    if run is None:
        return f"No cycle run recorded for {queue_date}."
    status, started, completed, metrics = run

    def get(stage: str, key: str) -> object:
        value = metrics.get(stage)
        return value.get(key, 0) if isinstance(value, dict) else "-"

    when = f"{started.astimezone():%H:%M}" + (f"–{completed.astimezone():%H:%M %Z}" if completed else " (still running)")
    errors = metrics.get("errors") or {}
    return (f"Cycle {queue_date}: {status}, {when}\n"
            f"Found {get('scrape', 'received')} ({get('scrape', 'new')} new); eligible {get('eligibility', 'eligible')}, "
            f"review {get('eligibility', 'review')}, filtered out {get('eligibility', 'ineligible')}\n"
            f"Scored {get('scoring', 'scored')}, queued {get('queue', 'queued')}, packets ready {get('prepare', 'ready')}, "
            f"Notion synced {get('notion', 'synced')}\n"
            f"Stage errors: {', '.join(errors) if isinstance(errors, dict) and errors else 'none'}")


def load_offset(path: Path = OFFSET_FILE) -> int:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def save_offset(offset: int, path: Path = OFFSET_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(str(offset))
    temporary.replace(path)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cycle_pid(lock: Path = LOCK_FILE) -> int | None:
    """PID of a running `jobagent run cycle` (lock file first, then pgrep), else None."""
    try:
        pid = int(lock.read_text().strip())
        if _alive(pid):
            return pid
    except (OSError, ValueError):
        pass
    try:
        found = subprocess.run(["pgrep", "-f", "jobagent(.cli)? run cycle"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    pids = [int(item) for item in found.stdout.split() if item.isdigit() and int(item) != os.getpid()]
    return pids[0] if pids else None


def fill_argv(database: str, target: int | str) -> list[str]:
    """A fixed argv: the job ID as an int, or one validated URL. Wrapped in caffeinate so the Mac stays awake."""
    base = [sys.executable, "-m", "jobagent.cli", "--database", database, "apply"]
    if isinstance(target, int):
        tail = ["fill", "--job-id", str(int(target)), "--unattended"]
    elif valid_url(target):
        tail = ["url", target, "--unattended"]
    else:
        raise ValueError("not a job ID or URL")
    awake = [caffeinate, "-i"] if (caffeinate := shutil.which("caffeinate")) else []
    return [*awake, *base, *tail]


def _popen_fill(argv: list[str], log_path: Path) -> subprocess.Popen[bytes]:
    with log_path.open("ab") as output:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)


@dataclass
class Bot:
    token: str
    chat_id: str
    repository: JobRepository
    database: str
    http: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=45))
    send: Callable[[str], None] | None = None
    importer: Callable[[JobRepository, str], object] | None = None
    prepare: Callable[[int], str] | None = None
    sync: Callable[[JobRepository, int], str] | None = None
    start_cycle: Callable[[], int] | None = None
    scan_inbox: Callable[[], InboxResult] | None = None  # dry run; no-change emails recorded, changes proposed
    fetch_email: Callable[[str], bytes | None] | None = None  # Message-ID -> raw bytes, read-only
    intents: IntentRouter | None = None  # plain-English messages
    find_cycle: Callable[[], int | None] = cycle_pid
    offset_path: Path = OFFSET_FILE
    context_path: Path | None = None  # None keeps the chat context in memory only
    logs: Path = Path("logs")
    urls: queue.Queue[str | int] = field(default_factory=queue.Queue)  # a link to import, or a job ID to prepare
    spawn_fill: Callable[[list[str], Path], subprocess.Popen[bytes]] = _popen_fill
    kill_group: Callable[[int, int], None] = os.killpg

    def __post_init__(self) -> None:
        self.send = self.send or TelegramNotifier(self.token, self.chat_id).send
        self.importer = self.importer or (lambda repository, url: UrlIntakeService(repository).import_url(url))
        self.prepare = self.prepare or self._prepare_subprocess
        self.sync = self.sync or _sync_notion
        self.start_cycle = self.start_cycle or self._spawn_cycle
        self.scan_inbox = self.scan_inbox or self._scan_inbox
        self.fetch_email = self.fetch_email or self._fetch_email
        self._inbox_busy = threading.Lock()
        self._cycle: subprocess.Popen[bytes] | None = None
        self._preparing: set[int] = set()  # job IDs queued or running in the worker
        self._preparing_lock = threading.Lock()
        self._fill: subprocess.Popen[bytes] | None = None  # one unattended fill at a time
        self._fill_label, self._fill_started, self._fill_cancelled = "", 0.0, False
        self._fill_lock = threading.Lock()
        self._outgoing = 0  # messages sent so far; a handler that sends none and returns nothing gets EMPTY_REPLY
        self.intents = self.intents or IntentRouter(self)

    # ---- Telegram I/O ---------------------------------------------------------------------
    def _api(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def redact(self, text: str) -> str:
        text = text.replace(self.token, "<token>") if self.token else text
        secret = os.getenv("WORKDAY_PASSWORD", "")
        return text.replace(secret, "[redacted]") if secret else text

    def reply(self, text: str) -> None:
        assert self.send is not None
        self._outgoing += 1
        self.send(trim(text))
        if self.intents is not None:
            self.intents.note("Bot", text)

    def send_plain(self, text: str) -> None:
        """Untrusted text (an email body): no parse_mode, so it cannot inject formatting; no link previews."""
        self._outgoing += 1
        response = self.http.post(self._api("sendMessage"), json={"chat_id": self.chat_id, "text": trim(text),
                                                                   "disable_web_page_preview": True}, timeout=20)
        response.raise_for_status()

    def send_document(self, path: Path, caption: str) -> None:
        self._outgoing += 1
        with path.open("rb") as handle:
            response = self.http.post(self._api("sendDocument"), data={"chat_id": self.chat_id, "caption": trim(caption, 1024)},
                                      files={"document": (path.name, handle, "application/pdf")}, timeout=120)
        response.raise_for_status()

    def get_updates(self, offset: int) -> list[dict]:
        response = self.http.get(self._api("getUpdates"), params={"offset": offset, "timeout": 30, "allowed_updates": '["message", "callback_query"]'}, timeout=45)
        response.raise_for_status()
        return response.json().get("result", [])

    # ---- dispatch -------------------------------------------------------------------------
    def handle_update(self, update: dict) -> None:
        if "callback_query" in update:  # an inline-button tap; same chat allowlist
            query = update["callback_query"]
            chat = ((query.get("message") or {}).get("chat") or {}).get("id")
            if str(chat) != str(self.chat_id):
                log.warning("Ignored Telegram callback %s from unauthorized chat %s", update.get("update_id"), chat)
                return
            assert self.intents is not None
            before = self._outgoing
            self.answer(self.intents.handle_callback(query), before)
            return
        message = update.get("message") or {}
        chat = (message.get("chat") or {}).get("id")
        if str(chat) != str(self.chat_id):
            log.warning("Ignored Telegram update %s from unauthorized chat %s", update.get("update_id"), chat)
            return
        text = message.get("text") or message.get("caption") or ""  # a photo's caption is its message
        if not text.strip() and (message.get("photo") or message.get("document")):
            self.reply(NO_IMAGES)
            return
        assert self.intents is not None
        self.intents.note("Master", text)
        before = self._outgoing
        self.answer(self.dispatch(text), before)

    def answer(self, reply: str | None, before: int) -> None:
        """Send the handler's reply; if it neither replied nor sent anything itself, say so instead of staying silent."""
        if reply and reply.strip():
            self.reply(reply)
        elif self._outgoing == before:
            self.reply(EMPTY_REPLY)

    def dispatch(self, text: str) -> str | None:
        command, args = parse_message(text)
        if command == "url":
            self.urls.put(args[0])
            ahead = self.urls.qsize() - 1
            return "on it…" + (f" ({ahead} link(s) ahead)" if ahead > 0 else "")
        handler = {"packet": self.cmd_packet, "prepare": self.cmd_prepare, "applied": self.cmd_applied, "status": self.cmd_status, "run": self.cmd_run,
                   "progress": self.cmd_progress, "fill": self.cmd_fill, "fillstatus": self.cmd_fillstatus, "cancel": self.cmd_cancel, "inbox": self.cmd_inbox, "email": self.cmd_email, "queue": self.cmd_queue, "help": lambda _: HELP, "start": lambda _: HELP}.get(command)
        if handler is None:
            if command == "unknown" and text.strip() and self.intents is not None:
                try:
                    response = self.http.post(self._api("sendChatAction"),
                                              json={"chat_id": self.chat_id, "action": "typing"}, timeout=5)
                    response.raise_for_status()
                except Exception as error:  # noqa: BLE001 - a typing indicator is best effort
                    log.warning("sendChatAction failed: %s", type(error).__name__)
                return self.intents.handle_text(text)  # plain English; links and slash commands never get here
            return "Unknown message. Send a job link or /help."
        return handler(args)

    def _job(self, args: list[str], usage: str) -> StoredJob | str:
        if not args or not args[0].lstrip("#").isdigit():
            return f"Usage: {usage}"
        job = self.repository.get_job(int(args[0].lstrip("#")))
        if job is None:
            return f"Job #{args[0].lstrip('#')} not found."
        if self.intents is not None:
            self.intents.set_last(job)
        return job

    def cmd_packet(self, args: list[str]) -> str | None:
        job = self._job(args, "/packet <id>")
        if isinstance(job, str):
            return job
        return self.send_packet(self.repository, job)

    def send_packet(self, repository: JobRepository, job: StoredJob) -> str | None:
        packet = repository.get_latest_ready_packet(job.id)
        if packet is None:
            assert self.intents is not None
            self.intents.offer_prepare(f"Job #{job.id}: no ready packet.", job)
            return None
        files = [Path(item) for item in (packet.resume_pdf_path, packet.cover_letter_pdf_path) if item]
        files = [path for path in files if path.is_file()]
        if not files:
            return f"Job #{job.id}: packet is ready but its PDFs are missing."
        for path in files:
            self.send_document(path, f"Job #{job.id}: {job.company} / {job.title}")
        return None if len(files) == 2 else f"Job #{job.id}: sent {len(files)} of 2 PDFs (no cover letter)."

    def cmd_prepare(self, args: list[str]) -> str:
        """Queue `jobagent prepare --job-id` on the worker (one at a time, like links)."""
        job = self._job(args, "/prepare <id>")
        if isinstance(job, str):
            return job
        with self._preparing_lock:
            if job.id in self._preparing:
                return f"Job #{job.id} is already being prepared."
            self._preparing.add(job.id)
        self.urls.put(job.id)
        ahead = self.urls.qsize() - 1
        return (f"Preparing the packet for job #{job.id} ({job.company} / {job.title}); this takes a few minutes. "
                "I'll send the PDFs when it's done." + (f" ({ahead} item(s) ahead)" if ahead > 0 else ""))

    def cmd_applied(self, args: list[str]) -> str:
        if not args or not args[0].lstrip("#").isdigit():
            return "Usage: /applied <id>"
        return self.cmd_status([args[0], "applied"])

    def cmd_status(self, args: list[str]) -> str:
        job = self._job(args, "/status <id> <state>")
        if isinstance(job, str):
            return job
        state = args[1].lower() if len(args) > 1 else ""
        if state not in APPLICATION_STATUSES:
            return f"Invalid state {state!r}. Valid: {', '.join(sorted(APPLICATION_STATUSES))}"
        self.repository.set_application_status(job.id, state, source="telegram")
        assert self.sync is not None
        return f"Job #{job.id} ({job.company} / {job.title}): {state}\n{self.sync(self.repository, job.id)}"

    def cmd_run(self, _: list[str]) -> str:
        pid = self._running()
        if pid:
            return f"A cycle is already running (PID {pid}). Try /progress."
        assert self.start_cycle is not None
        pid = self.start_cycle()
        return f"Cycle started at {datetime.now().astimezone():%H:%M %Z} (PID {pid}). Telegram will report its end."

    # ---- unattended fill (one background child at a time) -------------------------------
    def _fill_running(self) -> str | None:
        if self._fill is not None and self._fill.poll() is None:
            minutes = int((time.monotonic() - self._fill_started) // 60)
            return f"A fill is running: {self._fill_label} (PID {self._fill.pid}, {minutes} min). /fillstatus or /cancel"
        return None

    def _fill_blocker(self, job: StoredJob) -> str | None:
        if self.repository.get_latest_ready_packet(job.id) is None:
            return f"Job #{job.id} has no ready packet. /prepare {job.id} first."
        return self._fill_running()

    def cmd_fill(self, args: list[str]) -> str | None:
        """Ask ✅/❌, then fill one job unattended (draft, never submit)."""
        usage = "/fill <job id or job URL>"
        if args and valid_url(args[0]):
            if (running := self._fill_running()) is not None:
                return running
            url = args[0]
            question = f"Import, prepare and fill {url} on your Mac, then save a draft? JobAgent never submits."
            assert self.intents is not None
            self.intents.confirm(question, lambda: self.start_fill(url))
            return None
        job = self._job(args, usage)
        if isinstance(job, str):
            return job
        if (blocked := self._fill_blocker(job)) is not None:
            return blocked
        assert self.intents is not None
        self.intents.confirm(f"Fill #{job.id} {job.company} / {job.title} on your Mac and save a draft? "
                             "JobAgent never submits.", lambda: self.start_fill(job.id))
        return None

    def start_fill(self, target: int | str) -> str:
        """Start the child (after the ✅). Its own Telegram messages carry the draft link and summary."""
        if isinstance(target, int):
            job = self.repository.get_job(target)
            if job is None:
                return f"Job #{target} not found."
            if (blocked := self._fill_blocker(job)) is not None:
                return blocked
            label = f"#{job.id} {job.company} / {job.title}"
        else:
            label = target
        with self._fill_lock:
            if (running := self._fill_running()) is not None:
                return running
            self.logs.mkdir(exist_ok=True)
            process = self.spawn_fill(fill_argv(self.database, target), self.logs / "bot_fill.out")
            self._fill, self._fill_label, self._fill_started, self._fill_cancelled = process, label, time.monotonic(), False
        threading.Thread(target=self._watch_fill, args=(process,), daemon=True, name="fill-watch").start()
        return (f"Filling {label} on your Mac (PID {process.pid}). It clicks Next / Continue / Save and Continue only and "
                "stops at the final step; I'll send the link and what to review. /fillstatus, /cancel")

    def _watch_fill(self, process: subprocess.Popen[bytes]) -> None:
        """A child that failed before its own summary (no packet, import error, crash) still gets reported."""
        code = process.wait()
        if code == 0 or self._fill_cancelled or process is not self._fill:
            return
        try:
            self.reply(f"Fill {self._fill_label} ended with exit code {code}. Last log lines:\n{self._fill_tail()}")
        except Exception:  # noqa: BLE001
            log.warning("Could not send the fill failure reply")

    def _fill_tail(self, lines: int = 5) -> str:
        path = self.logs / "bot_fill.out"
        if not path.exists():
            return "(no log yet)"
        tail = path.read_text(errors="replace").splitlines()[-lines:]
        return self.redact("\n".join(line[:300] for line in tail)) or "(empty)"

    def cmd_fillstatus(self, _: list[str]) -> str:
        running = self._fill_running()
        head = running or (f"No fill running. Last: {self._fill_label}" if self._fill_label else "No fill running.")
        return f"{head}\n{self._fill_tail()}"

    def cmd_cancel(self, _: list[str]) -> str | None:
        if self._fill_running() is None:
            return "No fill running."
        assert self.intents is not None
        self.intents.confirm(f"Stop the fill of {self._fill_label}? Its browser closes; nothing is submitted.", self.cancel_fill)
        return None

    def cancel_fill(self) -> str:
        with self._fill_lock:
            process = self._fill
            if process is None or process.poll() is not None:
                return "No fill running."
            self._fill_cancelled = True
            try:
                self.kill_group(process.pid, signal.SIGTERM)  # start_new_session: the child leads its own group
            except ProcessLookupError:
                return "That fill had already ended."
        return f"Stopped the fill of {self._fill_label}. Nothing was submitted."

    def cmd_progress(self, _: list[str]) -> str:
        pid = self._running()
        path = self.logs / f"cycle_{datetime.now().astimezone().date().isoformat()}.log"
        try:
            tail = "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-10:])
        except OSError:
            tail = f"(no {path.name} yet)"
        return f"Cycle: {'running (PID ' + str(pid) + ')' if pid else 'not running'}\n{tail}"

    def cmd_queue(self, args: list[str]) -> str:
        """One day's pipeline (default today): the cycle's counts from SQLite, then that day's queue."""
        today = datetime.now().astimezone().date()
        word = args[0].lower() if args else "today"
        try:
            day = {"today": today, "yesterday": today - timedelta(days=1)}.get(word) or date.fromisoformat(word)
        except ValueError:
            return "Usage: /queue [today|yesterday|YYYY-MM-DD]"
        lines = []
        for entry in self.repository.list_queue(day.isoformat()):
            job = self.repository.get_job(entry.job_id)
            if job:
                lines.append(f"{entry.rank}. #{job.id} {job.company} / {job.title} ({entry.ranking_score:.1f})")
        queue = f"Queue {day}:\n" + "\n".join(lines) if lines else f"No queued jobs for {day}."
        return f"{cycle_summary(self.repository, day.isoformat())}\n\n{queue}"

    def cmd_inbox(self, _: list[str]) -> str:
        """Dry-run `inbox check` in the background, then ask ✅/❌ before applying its proposals."""
        if not self._inbox_busy.acquire(blocking=False):
            return "An inbox check is already running."

        def work() -> None:
            try:
                assert self.scan_inbox is not None and self.intents is not None
                result = self.scan_inbox()
                proposals, text = result.proposals, proposal_text(result)
                if not proposals:
                    self.reply(text)
                    return
                self.intents.confirm(text + "\nApply these changes?",
                                     lambda: self._apply_inbox(result.mailbox, proposals, approved=True),
                                     lambda: self._apply_inbox(result.mailbox, proposals, approved=False))
            except Exception as error:  # noqa: BLE001
                log.error("Inbox check failed: %s", self.redact(type(error).__name__))
                try:
                    self.reply("Inbox check failed; see the bot log.")
                except Exception:  # noqa: BLE001
                    log.warning("Could not send the failure reply")
            finally:
                self._inbox_busy.release()

        threading.Thread(target=work, daemon=True, name="inbox-check").start()
        return "Checking Gmail (dry run; I'll ask before changing anything)…"

    def cmd_email(self, args: list[str]) -> str:
        """Show one email from email_responses in full, fetched read-only in the background.

        Only recorded emails can be shown, so the bot is not a general mailbox reader. The body is
        untrusted: it is sent to Master as plain text and never reaches the AI router or any action."""
        if not args or not args[0].lstrip("#").isdigit():
            return "Usage: /email <email_id> (ids are listed when you ask about a company's emails)"
        email_id = int(args[0].lstrip("#"))
        row = self.repository.get_email_response(email_id)
        if row is None:
            return f"No recorded email #{email_id}. Only emails JobAgent has processed can be shown."
        hint = gmail_message_hint(row.message_id)

        def work() -> None:
            try:
                assert self.fetch_email is not None
                raw = self.fetch_email(row.message_id)
                if raw is None:
                    self.reply(f"Email #{email_id} is no longer in Gmail (deleted?). Gmail search: {hint}")
                    return
                for part in split_for_telegram(f"Email #{email_id}\n" + render_email(raw), hint):
                    self.send_plain(part)
            except Exception as error:  # noqa: BLE001
                log.error("Email fetch failed: %s", self.redact(type(error).__name__))
                try:
                    self.reply(f"Could not fetch email #{email_id}; see the bot log. Gmail search: {hint}")
                except Exception:  # noqa: BLE001
                    log.warning("Could not send the failure reply")

        threading.Thread(target=work, daemon=True, name="email-show").start()
        return f"Fetching email #{email_id} (read-only)…"

    def _fetch_email(self, message_id: str) -> bytes | None:
        reader = reader_from_env()
        if reader is None:
            raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set")
        return reader.fetch_one(message_id)

    def _apply_inbox(self, mailbox: str, proposals: list[Decision], *, approved: bool) -> str:
        """Apply exactly the proposals Master saw (no re-scan), or record them declined for review."""
        changed = record_decisions(self.repository, proposals, mailbox=mailbox, approved=approved)
        if not approved:
            return f"Cancelled: no status changed. {len(proposals)} email(s) moved to review (jobagent inbox review)."
        jobs = sorted(set(changed))
        if jobs:
            try:
                sync_notion_statuses(self.repository, notion_tracker(), jobs)
            except RuntimeError as error:
                log.warning("Inbox Notion sync skipped: %s", str(error)[:200])
        skipped = len(proposals) - len({d.email.message_id for d in proposals if d.job and d.job.id in jobs})
        return f"Applied {len(jobs)} status change(s)." + (f" {skipped} skipped (already recorded or no longer forward)." if skipped > 0 else "")

    def _running(self) -> int | None:
        if self._cycle is not None and self._cycle.poll() is None:  # poll() also reaps a finished child
            return self._cycle.pid
        return self.find_cycle()

    # ---- long work ------------------------------------------------------------------------
    def process_url(self, repository: JobRepository, url: str) -> str | None:
        """Import one link, prepare it (packet + Notion), and describe the result."""
        assert self.importer is not None and self.prepare is not None
        try:
            result = self.importer(repository, url)
        except IntakeError as error:
            return trim(f"Could not import {url}:\n{error}")
        job = result.job  # type: ignore[attr-defined]
        assert self.intents is not None
        self.intents.set_last(job)
        if not result.created:  # type: ignore[attr-defined]
            text = f"Duplicate: already in the database as job #{job.id}.\n{describe(job)}\nPacket: "
            if repository.get_latest_ready_packet(job.id):
                return text + f"ready\n/packet {job.id}"
            self.intents.offer_prepare(text + "not prepared", job)
            return None
        output = self.prepare(job.id)
        packet = repository.get_latest_ready_packet(job.id)
        status = "ready" + ("" if packet.cover_letter_pdf_path else " (no cover letter)") if packet else "not ready"
        tail = "\n".join(output.strip().splitlines()[-3:])
        return f"{describe(job)}\nPacket: {status}" + (f"\n{tail}" if packet is None and tail else "") + f"\n/packet {job.id}"

    def process_prepare(self, repository: JobRepository, job_id: int) -> str | None:
        """Build one job's packet, report it, then send its PDFs."""
        assert self.prepare is not None
        job = repository.get_job(job_id)
        if job is None:
            return f"Job #{job_id} not found."
        output = self.prepare(job_id)
        packet = repository.get_latest_ready_packet(job_id)
        if packet is None:
            tail = "\n".join(output.strip().splitlines()[-3:])
            return f"Job #{job_id} ({job.company} / {job.title}): packet not ready." + (f"\n{tail}" if tail else "")
        self.reply(f"Job #{job_id}: {job.company} / {job.title}\nPacket: ready" + ("" if packet.cover_letter_pdf_path else " (no cover letter)"))
        return self.send_packet(repository, job)

    def _prepare_subprocess(self, job_id: int) -> str:
        argv = [sys.executable, "-m", "jobagent.cli", "--database", self.database, "prepare", "--job-id", str(int(job_id))]
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=1800, stdin=subprocess.DEVNULL, check=False)
        except subprocess.TimeoutExpired:
            return "prepare timed out after 30 minutes"
        return done.stdout + done.stderr

    def _scan_inbox(self) -> InboxResult:
        """Runs in the inbox thread, so it opens its own SQLite engine."""
        reader = reader_from_env()
        if reader is None:
            raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set")
        repository = JobRepository(Path(self.database))
        result = scan_inbox(repository, reader, classifier=ClaudeEmailClassifier())
        propose_inbox(repository, result)
        return result

    def _spawn_cycle(self) -> int:
        self.logs.mkdir(exist_ok=True)
        with (self.logs / "bot_cycle.out").open("ab") as output:
            self._cycle = subprocess.Popen(
                [sys.executable, "-m", "jobagent.cli", "--database", self.database, "run", "cycle"],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
            )
        return self._cycle.pid

    def url_worker(self, repository_factory: Callable[[], JobRepository]) -> None:
        repository = repository_factory()  # a thread gets its own SQLite engine
        while True:
            item = self.urls.get()
            try:
                reply = self.process_prepare(repository, item) if isinstance(item, int) else self.process_url(repository, item)
                if reply:
                    self.reply(reply)
            except Exception as error:  # noqa: BLE001 - one bad item must not stop the worker
                log.error("Worker item failed: %s", self.redact(repr(error)))  # no traceback: it can carry the token URL
                try:
                    self.reply(f"Failed on {'job #' if isinstance(item, int) else ''}{item}: {self.redact(str(error))[:500]}")
                except Exception:  # noqa: BLE001
                    log.warning("Could not send the failure reply")
            finally:
                if isinstance(item, int):
                    with self._preparing_lock:
                        self._preparing.discard(item)
                self.urls.task_done()

    def poll_forever(self, repository_factory: Callable[[], JobRepository]) -> None:
        threading.Thread(target=self.url_worker, args=(repository_factory,), daemon=True, name="url-worker").start()
        offset = load_offset(self.offset_path)
        log.info("Telegram bot polling (offset %s)", offset)
        while True:
            try:
                updates = self.get_updates(offset)
            except (httpx.HTTPError, ValueError) as error:
                log.warning("getUpdates failed: %s", self.redact(str(error)))
                time.sleep(5)
                continue
            for update in updates:
                offset = int(update["update_id"]) + 1
                save_offset(offset, self.offset_path)  # saved first: a crashing message is never replayed
                try:
                    self.handle_update(update)
                except Exception as error:  # noqa: BLE001
                    log.error("Update %s failed: %s", update.get("update_id"), self.redact(repr(error)))
                    try:
                        self.reply(f"Error: {self.redact(str(error))[:500] or type(error).__name__}")
                    except Exception:  # noqa: BLE001
                        log.warning("Could not send the error reply")


def _sync_notion(repository: JobRepository, job_id: int) -> str:
    """Best effort per-job Notion sync, like `prepare --job-id`."""
    try:
        result = sync_to_notion(repository, notion_tracker(), [job_id])
    except RuntimeError as error:
        return f"Notion: not synced ({error})"
    errors = result["errors"]
    if errors:  # type: ignore[truthy-bool]
        return f"Notion: failed ({'; '.join(str(value) for value in errors.values())[:300]})"  # type: ignore[union-attr]
    return "Notion: synced" if result["synced"] else "Notion: nothing to sync (no ready packet)"


def run_bot(database: str) -> int:
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first.")
        return 1
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines contain the token in the URL
    bot = Bot(token, chat_id, JobRepository(Path(database)), database, context_path=CONTEXT_FILE)
    try:
        bot.poll_forever(lambda: JobRepository(Path(database)))
    except KeyboardInterrupt:
        print("\nBot stopped.")
    return 0
