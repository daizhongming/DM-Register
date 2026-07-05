# Repository Guidelines

## Project Structure & Module Organization

This repository is a Python automation tool with two entry points: CLI registration and a FastAPI WebUI. Core protocol code lives at the repository root:

- `register_outlook.py`: single-account CLI entry point.
- `auth_flow.py`, `sentinel.py`, `sentinel_quickjs.py`, `openai_sentinel_quickjs.js`: registration and Sentinel flow.
- `mail_outlook.py`, `mail_cf.py`, `sms_provider.py`: mail and SMS providers.
- `http_client.py`, `config.py`: shared HTTP/config helpers.
- `webui/`: FastAPI app, SQLite access, workers, static frontend assets.
- `data/`: local data files. Runtime DBs, logs, tokens, and account exports are ignored by `.gitignore`.

## Build, Test, and Development Commands

- `pip install -r requirements.txt`: install Python dependencies.
- `python start_webui.py --no-browser`: run the WebUI at `http://127.0.0.1:8765/`.
- `python start_webui.py --reload --no-browser`: run the WebUI with reload during development.
- `python register_outlook.py "email----password----client_id----refresh_token"`: run one CLI registration.
- `python -m py_compile *.py webui/*.py`: quick syntax check for changed Python files.

Node.js 18+ is recommended when using the QuickJS Sentinel path.

## Coding Style & Naming Conventions

Use Python 3 style already present in the repo: 4-space indentation, `snake_case` functions and variables, `PascalCase` classes, and module-level constants in uppercase. Prefer small functions near their caller unless the same logic is already shared elsewhere. Keep imports explicit and avoid adding dependencies unless `requirements.txt` already covers the need.

## Testing Guidelines

No formal test suite is present in this checkout. For now, run `python -m py_compile *.py webui/*.py` before committing Python changes. If adding tests, place them under `tests/` with names like `test_sms_provider.py`, and keep network-dependent tests opt-in or mocked.

## Commit & Pull Request Guidelines

This checkout has no local `.git` history, so no project-specific commit convention can be inferred. Use short imperative commit messages, for example `Fix WebUI account reset state`. Pull requests should include a brief description, manual test commands run, linked issues if any, and screenshots for visible WebUI changes.

## Security & Configuration Tips

Do not commit credentials, account exports, local databases, logs, trace dumps, `.env` files, or `config.local.json`. These are already ignored for a reason. Redact Outlook refresh tokens, SMS API keys, cookies, and OpenAI tokens from issues, logs, and screenshots.
