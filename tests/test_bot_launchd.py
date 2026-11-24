import plistlib
from pathlib import Path

from jobagent.runtime import bot_launchd_plist


def test_bot_launchd_plist_is_always_on_and_secret_free() -> None:
    data = plistlib.loads(bot_launchd_plist(Path("/repo"), Path("/bin/uv"), "/bin:/usr/bin").encode())
    assert data["ProgramArguments"] == ["/bin/uv", "run", "jobagent", "bot"]
    assert data["RunAtLoad"] is True and data["KeepAlive"] is True and data["ThrottleInterval"] == 30
    assert data["AbandonProcessGroup"] is True  # a bot restart leaves a running /fill alone
    assert data["WorkingDirectory"] == "/repo" and data["StandardErrorPath"] == "/repo/logs/bot.err.log"
    assert data["EnvironmentVariables"] == {"PATH": "/bin:/usr/bin"}
