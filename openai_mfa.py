"""Helpers for OpenAI account MFA setup."""
from __future__ import annotations

import json
import shutil
import subprocess
from http.cookies import SimpleCookie
from pathlib import Path

MFA_SETTINGS_URL = "https://chatgpt.com/#settings/Security"


def playwright_storage_state(cred: dict) -> dict:
    seen = set()
    cookies = []

    def add_cookie(name: str, value: str, http_only: bool = False):
        name = (name or "").strip()
        value = (value or "").strip()
        if not name or not value or name in seen:
            return
        seen.add(name)
        cookies.append({
            "name": name,
            "value": value,
            "domain": ".chatgpt.com",
            "path": "/",
            "expires": -1,
            "httpOnly": http_only,
            "secure": True,
            "sameSite": "Lax",
        })

    jar = SimpleCookie()
    try:
        jar.load(cred.get("cookie_header") or "")
    except Exception:
        jar = SimpleCookie()
    for name, morsel in jar.items():
        add_cookie(name, morsel.value, http_only=name.startswith("__Secure-next-auth"))
    add_cookie("__Secure-next-auth.session-token", cred.get("session_token") or "", http_only=True)
    if not cookies:
        raise ValueError("missing session_token/cookie_header")
    return {"cookies": cookies, "origins": []}


def launch_mfa_setup_browser(
    cred: dict,
    email: str,
    *,
    root: Path,
    proxy: str = "",
    url: str = MFA_SETTINGS_URL,
) -> dict:
    npx = shutil.which("npx")
    if not npx:
        raise RuntimeError("npx not found; install Node.js/npm first")

    state_dir = root / "data" / "playwright_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    safe_email = "".join(ch if ch.isalnum() else "_" for ch in email.lower())[:90]
    storage_path = state_dir / f"openai_mfa_{safe_email}.json"
    storage_path.write_text(json.dumps(playwright_storage_state(cred), ensure_ascii=False), encoding="utf-8")

    cmd = [
        npx, "--yes", "playwright", "open",
        "--load-storage", str(storage_path),
        "--save-storage", str(storage_path),
    ]
    if proxy.strip():
        cmd.extend(["--proxy-server", proxy.strip()])
    cmd.append(url.strip() or MFA_SETTINGS_URL)
    proc = subprocess.Popen(cmd, cwd=str(root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"pid": proc.pid, "url": cmd[-1], "storage_path": str(storage_path)}
