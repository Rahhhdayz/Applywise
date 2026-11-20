from __future__ import annotations

import os

from jobagent.environment import load_local_env


def test_local_env_loads_without_overriding_shell(tmp_path, monkeypatch) -> None:
    path = tmp_path / ".env"
    path.write_text("ONE=from-file\nTWO='quoted value'\n# ignored\n")
    monkeypatch.setenv("ONE", "from-shell")
    load_local_env(path)
    assert os.environ["ONE"] == "from-shell"
    assert os.environ["TWO"] == "quoted value"
