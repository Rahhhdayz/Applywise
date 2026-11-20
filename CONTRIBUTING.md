# Contributing

Thanks for your interest! Applywise is a personal project, but issues and small pull requests are welcome.

1. Install [uv](https://docs.astral.sh/uv/) and Python 3.12, then run `uv sync`.
2. Make your change with type hints and small, focused modules. Keep Python deterministic, and use AI only where
   semantic judgment is needed.
3. Add or update tests. External services must be mocked, and tests must never read real candidate data.
4. Run `uv run ruff check .` and `uv run pytest -q` before you open a pull request.

Ground rules:

- Never commit credentials, `.env`, databases, logs, or real candidate files. Only `*.example.yaml` templates
  belong in `candidate/` and `config/`.
- Treat job descriptions and web pages as untrusted data.
- Nothing may submit an application or send email on the user's behalf.
