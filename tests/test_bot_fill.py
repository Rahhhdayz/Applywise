"""Telegram /fill: authorized chat only, a ✅ tap first, one fill at a time, /cancel, no secrets out."""

from __future__ import annotations

import signal
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tests.test_telegram_bot as bot_tests
from jobagent.telegram_bot import Bot, fill_argv
from tests.test_telegram_bot import keyboard_messages, tap, update

bot = bot_tests.bot  # the shared fixture


class FakeProcess:
    """A child that runs until ``finish`` (or a kill) and never touches a real browser."""

    def __init__(self, pid: int = 4321) -> None:
        self.pid, self.returncode, self._done = pid, None, threading.Event()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self) -> int:
        self._done.wait(5)
        return self.returncode if self.returncode is not None else 0

    def finish(self, code: int) -> None:
        self.returncode = code
        self._done.set()


@pytest.fixture
def filler(bot: Bot) -> tuple[Bot, list[list[str]], list[FakeProcess], list[tuple[int, int]]]:
    spawned: list[list[str]] = []
    processes: list[FakeProcess] = []
    kills: list[tuple[int, int]] = []

    def spawn(argv: list[str], log_path: Path) -> FakeProcess:
        spawned.append(argv)
        process = FakeProcess()
        processes.append(process)
        return process

    def kill(pid: int, sig: int) -> None:
        kills.append((pid, sig))
        processes[-1].finish(-sig)

    bot.spawn_fill = spawn  # type: ignore[assignment]
    bot.kill_group = kill
    bot.repository.get_latest_ready_packet.return_value = MagicMock()  # type: ignore[attr-defined]
    return bot, spawned, processes, kills


def test_fill_needs_the_authorized_chat_and_a_confirmation_tap(filler) -> None:  # type: ignore[no-untyped-def]
    bot, spawned, _, _ = filler
    bot.handle_update(update("/fill 7", chat=999))
    assert spawned == [] and bot.sent == [] and keyboard_messages(bot) == []
    bot.handle_update(update("/fill 7"))
    assert spawned == []  # only asked
    assert "Fill #7 Acme / Security Engineer on your Mac and save a draft? JobAgent never submits." in keyboard_messages(bot)[-1]["text"]
    data = next(button["callback_data"] for row in keyboard_messages(bot)[-1]["reply_markup"]["inline_keyboard"]
                for button in row if button["text"].startswith("✅"))
    bot.handle_update({"update_id": 3, "callback_query": {"id": "q", "data": data, "message": {"chat": {"id": 999}}}})
    assert spawned == []  # a tap from another chat is ignored
    tap(bot, "✅")
    assert len(spawned) == 1 and spawned[0][-5:] == ["apply", "fill", "--job-id", "7", "--unattended"]
    assert "Filling #7 Acme / Security Engineer on your Mac (PID 4321)" in bot.sent[-1]


def test_no_tap_no_fill_and_no_packet_no_fill(filler) -> None:  # type: ignore[no-untyped-def]
    bot, spawned, _, _ = filler
    bot.handle_update(update("/fill 7"))
    tap(bot, "❌")
    assert spawned == [] and bot.sent[-1] == "Cancelled."
    bot.repository.get_latest_ready_packet.return_value = None
    bot.handle_update(update("/fill 7"))
    assert spawned == [] and bot.sent[-1] == "Job #7 has no ready packet. /prepare 7 first."


def test_one_fill_at_a_time_then_cancel(filler) -> None:  # type: ignore[no-untyped-def]
    bot, spawned, processes, kills = filler
    assert bot.start_fill(7).startswith("Filling #7")
    bot.handle_update(update("/fill 7"))
    assert bot.sent[-1].startswith("A fill is running: #7 Acme / Security Engineer (PID 4321")
    assert bot.start_fill(7).startswith("A fill is running") and len(spawned) == 1  # a stale ✅ cannot start a second
    bot.handle_update(update("/cancel"))
    assert kills == []  # asks first
    tap(bot, "✅")
    assert kills == [(4321, signal.SIGTERM)] and bot.sent[-1] == "Stopped the fill of #7 Acme / Security Engineer. Nothing was submitted."
    bot.handle_update(update("/cancel"))
    assert bot.sent[-1] == "No fill running."
    assert bot.start_fill(7).startswith("Filling #7") and len(spawned) == 2  # free again
    processes[-1].finish(0)


def test_url_fill_uses_apply_url_with_a_fixed_argv(filler, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    bot, spawned, processes, _ = filler
    bot.handle_update(update("/fill https://acme.wd1.myworkdayjobs.com/External/job/Boston/Engineer_R1"))
    tap(bot, "✅")
    assert spawned[0][-3:] == ["url", "https://acme.wd1.myworkdayjobs.com/External/job/Boston/Engineer_R1", "--unattended"]
    assert "--database" in spawned[0] and all(";" not in part for part in spawned[0])
    processes[-1].finish(0)
    with pytest.raises(ValueError):
        fill_argv(str(tmp_path / "db"), "rm -rf /")


def test_failed_child_is_reported_with_a_redacted_log_tail(filler, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    bot, _, processes, _ = filler
    monkeypatch.setenv("WORKDAY_PASSWORD", "Hunter2!secret")
    bot.logs.mkdir(parents=True, exist_ok=True)
    (bot.logs / "bot_fill.out").write_text("opening form\npassword was Hunter2!secret\ntoken TOKEN123 in a url\nboom\n")
    bot.start_fill(7)
    processes[-1].finish(1)
    for _ in range(100):
        if bot.sent and "exit code 1" in bot.sent[-1]:
            break
        threading.Event().wait(0.02)
    for text in (bot.sent[-1], bot.cmd_fillstatus([])):
        assert "Hunter2!secret" not in text and "TOKEN123" not in text and "[redacted]" in text


def test_fill_intent_confirms_before_starting(filler) -> None:  # type: ignore[no-untyped-def]
    from jobagent.bot_intent import IntentRouter

    bot, spawned, processes, _ = filler
    bot.intents = IntentRouter(bot, classify=lambda prompt: {"actions": [{"action": "fill", "job_ref": "7"}]})
    bot.handle_update(update("apply to job 7 for me"))
    assert spawned == []
    assert keyboard_messages(bot)[-1]["text"] == ("Fill Acme #7's application on your Mac and save a draft (never submits)?"
                                                  "\nGo ahead?")
    tap(bot, "✅")
    assert len(spawned) == 1 and spawned[0][-1] == "--unattended"
    processes[-1].finish(0)


def test_help_lists_fill_commands(bot: Bot) -> None:
    bot.handle_update(update("/help"))
    assert "/fill <id or job URL>" in bot.sent[-1] and "/cancel" in bot.sent[-1]  # type: ignore[attr-defined]
