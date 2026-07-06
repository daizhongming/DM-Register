"""SQLite 号池 + 注册结果存储。

表结构：
  outlook_accounts: 接码号池（4 段格式入库 + 状态机）
  registered:       注册成功结果（凭证 JSON）
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent / "webui.db"
K12_WORKSPACE_FAILURES_KEY = "k12_workspace_failures"
K12_USABLE_WORKSPACES_KEY = "k12_usable_workspace_ids_by_mail_source"
PAYMESH_CARD_STATE_KEY = "paymesh_card_state"
MAIL_SOURCES = ("outlook", "cf_temp", "paymesh_card")

_lock = threading.Lock()  # SQLite 写入串行化


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    con = _conn()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS outlook_accounts (
            email           TEXT PRIMARY KEY,
            password        TEXT,
            client_id       TEXT,
            refresh_token   TEXT,
            status          TEXT NOT NULL DEFAULT 'available',
                            -- available / in_use / done / failed
            imported_at     REAL,
            claimed_at      REAL,
            finished_at     REAL,
            fail_reason     TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_outlook_status ON outlook_accounts(status);

        CREATE TABLE IF NOT EXISTS settings (
            key     TEXT PRIMARY KEY,
            value   TEXT
        );

        CREATE TABLE IF NOT EXISTS registered (
            email           TEXT PRIMARY KEY,
            password        TEXT,
            access_token    TEXT,
            session_token   TEXT,
            refresh_token   TEXT,
            id_token        TEXT,
            device_id       TEXT,
            csrf_token      TEXT,
            cookie_header   TEXT,
            extra_json      TEXT,
            created_at      REAL
        );

        CREATE TABLE IF NOT EXISTS runs (
            run_id          TEXT PRIMARY KEY,
            email           TEXT,
            status          TEXT,        -- running / done / failed
            started_at      REAL,
            finished_at     REAL,
            log_path        TEXT,
            error           TEXT,
            error_category  TEXT         -- network / account / unknown
        );

        CREATE TABLE IF NOT EXISTS account_auth_resources (
            email                   TEXT PRIMARY KEY,
            mail_source             TEXT,
            paymesh_card_code       TEXT,
            paymesh_session_status  TEXT,
            sms_provider            TEXT,
            sms_service             TEXT,
            sms_country             TEXT,
            phone_number            TEXT,
            sms_activation_id       TEXT,
            sms_status              TEXT,
            last_reauth_method      TEXT,
            last_reauth_at          REAL,
            last_reauth_error       TEXT,
            created_at              REAL,
            updated_at              REAL
        );
    """)
    con.commit()
    # 老 DB migrate：error_category 在后期才加，对已建表补列
    cur = con.execute("PRAGMA table_info(runs)")
    cols = {r[1] for r in cur.fetchall()}
    if "error_category" not in cols:
        con.execute("ALTER TABLE runs ADD COLUMN error_category TEXT")
        con.commit()


# ──────────────────────── outlook 号池 ────────────────────────


def parse_lines(text: str) -> list[dict]:
    """解析 4 段格式（每行一个）。无效行跳过。"""
    out: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        if len(parts) != 4:
            continue
        email, password, client_id, refresh = (p.strip() for p in parts)
        if "@" not in email or len(refresh) < 20:
            continue
        out.append({
            "email": email.lower(),
            "password": password,
            "client_id": client_id,
            "refresh_token": refresh,
        })
    return out


def import_accounts(text: str) -> dict:
    """批量入库。已存在的 email 仅在 refresh_token 不同时更新。"""
    rows = parse_lines(text)
    now = time.time()
    inserted = updated = skipped = 0
    with _lock:
        con = _conn()
        for r in rows:
            cur = con.execute(
                "SELECT refresh_token FROM outlook_accounts WHERE email=?",
                (r["email"],),
            )
            existing = cur.fetchone()
            if existing is None:
                con.execute(
                    "INSERT INTO outlook_accounts(email, password, client_id, refresh_token, "
                    "status, imported_at) VALUES (?, ?, ?, ?, 'available', ?)",
                    (r["email"], r["password"], r["client_id"], r["refresh_token"], now),
                )
                inserted += 1
            elif existing["refresh_token"] != r["refresh_token"]:
                con.execute(
                    "UPDATE outlook_accounts SET refresh_token=?, password=?, client_id=?, "
                    "status='available', imported_at=?, fail_reason=NULL WHERE email=?",
                    (r["refresh_token"], r["password"], r["client_id"], now, r["email"]),
                )
                updated += 1
            else:
                skipped += 1
        con.commit()
    return {"parsed": len(rows), "inserted": inserted, "updated": updated, "skipped": skipped}


def list_accounts(status: str = "", limit: int = 500) -> list[dict]:
    con = _conn()
    if status:
        cur = con.execute(
            "SELECT * FROM outlook_accounts WHERE status=? ORDER BY imported_at DESC LIMIT ?",
            (status, limit),
        )
    else:
        cur = con.execute(
            "SELECT * FROM outlook_accounts ORDER BY imported_at DESC LIMIT ?",
            (limit,),
        )
    return [dict(r) for r in cur.fetchall()]


def get_account(email: str) -> Optional[dict]:
    con = _conn()
    cur = con.execute("SELECT * FROM outlook_accounts WHERE email=?", (email.lower(),))
    row = cur.fetchone()
    return dict(row) if row else None


def claim_account(email: str) -> Optional[dict]:
    """原子 claim 指定邮箱（available / failed -> in_use）。

    failed 也允许重试 claim：之前 OpenAI 风控误判 / 网络抖动等导致 fail 的号
    应允许用户手动重试，已 done 的号才禁止重 claim（防误覆盖凭证）。
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    with _lock:
        con = _conn()
        cur = con.execute(
            "SELECT * FROM outlook_accounts WHERE email=? AND status IN ('available', 'failed')",
            (email,),
        )
        row = cur.fetchone()
        if not row:
            return None
        rc = con.execute(
            "UPDATE outlook_accounts SET status='in_use', claimed_at=?, fail_reason=NULL "
            "WHERE email=? AND status IN ('available', 'failed')",
            (time.time(), email),
        )
        con.commit()
        if rc.rowcount != 1:
            return None
        return dict(row)


def claim_next() -> Optional[dict]:
    """原子 claim 任一 available 号。"""
    with _lock:
        con = _conn()
        cur = con.execute(
            "SELECT * FROM outlook_accounts WHERE status='available' "
            "ORDER BY imported_at ASC LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            return None
        rc = con.execute(
            "UPDATE outlook_accounts SET status='in_use', claimed_at=? "
            "WHERE email=? AND status='available'",
            (time.time(), row["email"]),
        )
        con.commit()
        if rc.rowcount != 1:
            return claim_next()
        return dict(row)


def mark_done(email: str) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='done', finished_at=?, fail_reason=NULL WHERE email=?",
            (time.time(), email.lower()),
        )
        con.commit()


def mark_failed(email: str, reason: str = "") -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='failed', finished_at=?, fail_reason=? WHERE email=?",
            (time.time(), (reason or "")[:500], email.lower()),
        )
        con.commit()


def mark_skipped(email: str, reason: str = "") -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='skipped', finished_at=?, fail_reason=? WHERE email=?",
            (time.time(), (reason or "")[:500], email.lower()),
        )
        con.commit()


def release_unused(email: str) -> None:
    """claim 后没真注册（异常 / 用户取消）→ 还回 available。"""
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL "
            "WHERE email=? AND status='in_use'",
            (email.lower(),),
        )
        con.commit()


def reset_to_available(email: str) -> bool:
    """手动重置单个号：done / failed → available，清空时间戳和失败原因。

    场景：注册成功但 refresh_token 没拿到，主人想重新跑一遍这个号。
    """
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL, "
            "finished_at=NULL, fail_reason=NULL "
            "WHERE lower(email)=lower(?)",
            (email,),
        )
        con.commit()
        return rc.rowcount > 0


def bulk_reset_to_available(emails: list[str]) -> int:
    """批量重置多个号。返回实际被改的行数。"""
    if not emails:
        return 0
    with _lock:
        con = _conn()
        rc = con.execute(
            f"UPDATE outlook_accounts SET status='available', claimed_at=NULL, "
            f"finished_at=NULL, fail_reason=NULL "
            f"WHERE lower(email) IN ({','.join(['lower(?)'] * len(emails))})",
            emails,
        )
        con.commit()
        return rc.rowcount


def reset_failed_to_available() -> int:
    """把所有 failed 号一次性重置为 available（清掉 fail_reason）。返回受影响行数。

    场景：代理短暂抽风导致一波号被冤枉标 failed，主人想给它们一次机会。
    """
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', fail_reason=NULL, "
            "finished_at=NULL WHERE status='failed'"
        )
        con.commit()
        return rc.rowcount


def release_stale_in_use(stale_seconds: float = 1800) -> int:
    """把 claimed_at 超过 N 秒还在 in_use 的号释放回 available。

    场景：上次 webui 强退/进程崩溃，号卡在 in_use 永远不释放。默认 30 分钟。
    """
    with _lock:
        con = _conn()
        cutoff = time.time() - stale_seconds
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL "
            "WHERE status='in_use' AND (claimed_at IS NULL OR claimed_at < ?)",
            (cutoff,),
        )
        con.commit()
        return rc.rowcount


def delete_account(email: str) -> bool:
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM outlook_accounts WHERE email=?", (email.lower(),))
        con.commit()
        return rc.rowcount > 0


def delete_accounts_by_status(status: str) -> int:
    """按状态批量删除。status 必须是 available/in_use/done/failed 之一；
    传 'all' 删全部。返回受影响行数。"""
    valid = {"available", "in_use", "done", "failed", "skipped", "all"}
    s = (status or "").strip().lower()
    if s not in valid:
        return 0
    with _lock:
        con = _conn()
        if s == "all":
            rc = con.execute("DELETE FROM outlook_accounts")
        else:
            rc = con.execute("DELETE FROM outlook_accounts WHERE status=?", (s,))
        con.commit()
        return rc.rowcount


def delete_accounts_by_emails(emails: list[str]) -> int:
    """按 email 列表批量删除。返回受影响行数。"""
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        placeholders = ",".join("?" * len(cleaned))
        rc = con.execute(
            f"DELETE FROM outlook_accounts WHERE email IN ({placeholders})",
            cleaned,
        )
        con.commit()
        return rc.rowcount


def stats() -> dict:
    con = _conn()
    cur = con.execute(
        "SELECT status, COUNT(*) AS n FROM outlook_accounts GROUP BY status"
    )
    out = {"available": 0, "in_use": 0, "done": 0, "failed": 0, "skipped": 0, "total": 0}
    for r in cur.fetchall():
        out[r["status"]] = r["n"]
        out["total"] += r["n"]
    return out


# ──────────────────────── 注册结果存储 ────────────────────────


def save_registered(d: dict) -> None:
    """保存注册成功（或部分成功）的凭证。覆盖同邮箱旧记录。

    凭证三件套（access_token / session_token / refresh_token）单独存列；
    其余字段（如 device_id / cookie_header / id_token / 自定义元数据）打包进 extra_json。
    """
    email = (d.get("email") or "").lower()
    if not email:
        return
    extra = {k: v for k, v in d.items() if k not in {
        "email", "password", "access_token", "session_token", "refresh_token",
        "id_token", "device_id", "csrf_token", "cookie_header",
    }}
    with _lock:
        con = _conn()
        con.execute(
            "INSERT OR REPLACE INTO registered "
            "(email, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, extra_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                email,
                d.get("password", ""),
                d.get("access_token", ""),
                d.get("session_token", ""),
                d.get("refresh_token", ""),
                d.get("id_token", ""),
                d.get("device_id", ""),
                d.get("csrf_token", ""),
                d.get("cookie_header", ""),
                json.dumps(extra, ensure_ascii=False) if extra else None,
                time.time(),
            ),
        )
        con.commit()


def update_registered_access_token(email: str, access_token: str) -> None:
    """更新已注册账号的 access_token（用于 K12 workspace AT）。"""
    email = (email or "").lower()
    if not email:
        return
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE registered SET access_token=? WHERE email=?",
            (access_token, email),
        )
        con.commit()


def list_registered(limit: int = 500) -> list[dict]:
    con = _conn()
    cur = con.execute(
        "SELECT email, password, "
        "length(access_token) AS at_len, length(session_token) AS st_len, "
        "length(refresh_token) AS rt_len, created_at FROM registered "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


def list_registered_full(limit: int = 5000) -> list[dict]:
    """返回完整凭证（用于批量导出）。每行同 get_registered 的格式。"""
    con = _conn()
    cur = con.execute(
        "SELECT * FROM registered ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    out = []
    for row in cur.fetchall():
        d = dict(row)
        if d.get("extra_json"):
            try:
                d["extra"] = json.loads(d["extra_json"])
            except Exception:
                d["extra"] = {}
        d.pop("extra_json", None)
        out.append(d)
    return out


def get_registered(email: str) -> Optional[dict]:
    con = _conn()
    cur = con.execute("SELECT * FROM registered WHERE email=?", (email.lower(),))
    row = cur.fetchone()
    if not row:
        return None
    out = dict(row)
    if out.get("extra_json"):
        try:
            out["extra"] = json.loads(out["extra_json"])
        except Exception:
            out["extra"] = {}
    out.pop("extra_json", None)
    return out


def delete_registered(email: str) -> bool:
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM registered WHERE email=?", (email.lower(),))
        con.commit()
        return rc.rowcount > 0


def delete_registered_by_emails(emails: list[str]) -> int:
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        placeholders = ",".join("?" * len(cleaned))
        rc = con.execute(
            f"DELETE FROM registered WHERE email IN ({placeholders})",
            cleaned,
        )
        con.commit()
        return rc.rowcount


def delete_all_registered() -> int:
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM registered")
        con.commit()
        return rc.rowcount


# ──────────────────────── 运行记录 ────────────────────────


def create_run(run_id: str, email: str, log_path: str) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO runs(run_id, email, status, started_at, log_path) "
            "VALUES (?, ?, 'running', ?, ?)",
            (run_id, email.lower(), time.time(), log_path),
        )
        con.commit()


def finish_run(run_id: str, status: str, error: str = "", category: str = "") -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE runs SET status=?, finished_at=?, error=?, error_category=? WHERE run_id=?",
            (status, time.time(), (error or "")[:500], category or None, run_id),
        )
        con.commit()


def list_runs(limit: int = 50) -> list[dict]:
    con = _conn()
    cur = con.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


# ──────────────────────── settings (KV) ────────────────────────


def get_setting(key: str, default: str = "") -> str:
    con = _conn()
    cur = con.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row["value"] if row else default


def _get_setting_con(con: sqlite3.Connection, key: str, default: str = "") -> str:
    cur = con.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row["value"] if row else default


def set_setting(key: str, value) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        con.commit()


# ──────────────────────── 邮箱来源配置 ────────────────────────


def _normalize_mail_source(mail_source: str) -> str:
    mail_source = str(mail_source or "").strip().lower()
    return mail_source if mail_source in MAIL_SOURCES else "outlook"


def _dedupe_workspace_ids(value) -> list[str]:
    rows = value.splitlines() if isinstance(value, str) else (value or [])
    out: list[str] = []
    seen: set[str] = set()
    for item in rows:
        ws_id = str(item or "").strip().lower()
        if not ws_id or ws_id.startswith("#") or ws_id in seen:
            continue
        seen.add(ws_id)
        out.append(ws_id)
    return out


def _parse_k12_usable_workspaces(raw: str) -> dict[str, list[str]]:
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {source: _dedupe_workspace_ids(data.get(source, [])) for source in MAIL_SOURCES}


def get_k12_usable_workspace_ids_by_mail_source() -> dict[str, list[str]]:
    return _parse_k12_usable_workspaces(get_setting(K12_USABLE_WORKSPACES_KEY, ""))


def get_k12_usable_workspace_ids(mail_source: str) -> list[str]:
    return get_k12_usable_workspace_ids_by_mail_source()[_normalize_mail_source(mail_source)]


def add_k12_usable_workspace_ids(mail_source: str, workspace_ids) -> list[str]:
    source = _normalize_mail_source(mail_source)
    with _lock:
        con = _conn()
        data = _parse_k12_usable_workspaces(_get_setting_con(con, K12_USABLE_WORKSPACES_KEY, ""))
        data[source] = _dedupe_workspace_ids(data[source] + _dedupe_workspace_ids(workspace_ids))
        con.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (K12_USABLE_WORKSPACES_KEY, json.dumps(data, ensure_ascii=False)),
        )
        con.commit()
    return data[source]


_AUTH_RESOURCE_FIELDS = {
    "mail_source",
    "paymesh_card_code",
    "paymesh_session_status",
    "sms_provider",
    "sms_service",
    "sms_country",
    "phone_number",
    "sms_activation_id",
    "sms_status",
    "last_reauth_method",
    "last_reauth_at",
    "last_reauth_error",
}


def _upsert_auth_resources_con(con: sqlite3.Connection, email: str, **fields) -> None:
    email = (email or "").strip().lower()
    if not email:
        return
    clean = {k: v for k, v in fields.items() if k in _AUTH_RESOURCE_FIELDS and v is not None}
    now = time.time()
    cols = ["email", "created_at", "updated_at", *clean.keys()]
    vals = [email, now, now, *clean.values()]
    placeholders = ",".join("?" for _ in cols)
    updates = ["updated_at=excluded.updated_at"]
    updates.extend(f"{k}=excluded.{k}" for k in clean)
    con.execute(
        f"INSERT INTO account_auth_resources({','.join(cols)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(email) DO UPDATE SET {','.join(updates)}",
        vals,
    )


def upsert_auth_resources(email: str, **fields) -> None:
    with _lock:
        con = _conn()
        _upsert_auth_resources_con(con, email, **fields)
        con.commit()


def _get_paymesh_card_by_email_con(con: sqlite3.Connection, email: str) -> Optional[dict]:
    email = (email or "").strip().lower()
    if not email:
        return None
    state = _read_paymesh_card_state(con)
    rows = []
    for code, rec in state.items():
        rec = dict(rec or {})
        if (rec.get("email") or "").strip().lower() != email:
            continue
        rows.append({
            "code": code,
            "card": _mask_paymesh_card(code),
            "email": email,
            "status": rec.get("status", ""),
            "session_status": rec.get("session_status", ""),
            "updated_at": rec.get("updated_at") or rec.get("finished_at") or rec.get("claimed_at") or 0,
            "fail_reason": rec.get("fail_reason", ""),
        })
    if not rows:
        return None
    rank = {"done": 0, "in_use": 1, "available": 2, "failed": 3}
    rows.sort(key=lambda r: (rank.get(r.get("status") or "", 9), -float(r.get("updated_at") or 0)))
    return rows[0]


def get_paymesh_card_by_email(email: str) -> Optional[dict]:
    with _lock:
        con = _conn()
        return _get_paymesh_card_by_email_con(con, email)


def get_auth_resources(email: str) -> dict:
    email = (email or "").strip().lower()
    if not email:
        return {}
    with _lock:
        con = _conn()
        paymesh = _get_paymesh_card_by_email_con(con, email)
        row = con.execute(
            "SELECT * FROM account_auth_resources WHERE email=?",
            (email,),
        ).fetchone()
        if paymesh and (not row or not row["paymesh_card_code"]):
            _upsert_auth_resources_con(
                con,
                email,
                mail_source="paymesh_card",
                paymesh_card_code=paymesh["code"],
                paymesh_session_status=paymesh.get("session_status", ""),
            )
            con.commit()
            row = con.execute(
                "SELECT * FROM account_auth_resources WHERE email=?",
                (email,),
            ).fetchone()
    out = dict(row) if row else {"email": email}
    if out.get("paymesh_card_code"):
        out["paymesh_card"] = _mask_paymesh_card(out["paymesh_card_code"])
    return out


def _safe_auth_resources(resources: dict) -> dict:
    safe = dict(resources or {})
    code = safe.pop("paymesh_card_code", "")
    if code and not safe.get("paymesh_card"):
        safe["paymesh_card"] = _mask_paymesh_card(code)
    return safe


def describe_reauth_capability(email: str, ignore_registered_rt: bool = False) -> dict:
    email = (email or "").strip().lower()
    resources = get_auth_resources(email)
    registered = get_registered(email) if email else None
    outlook = get_account(email) if email else None
    sms_cfg = get_sms_internal_config()
    sms_ready = bool(sms_cfg.get("sms_enabled") and sms_cfg.get("sms_api_key"))
    blockers: list[str] = []
    warnings: list[str] = []
    method = ""

    if not email:
        blockers.append("missing_email")
    elif registered and (registered.get("refresh_token") or "").strip() and not ignore_registered_rt:
        method = "registered_refresh_token"
    else:
        mail_source = (resources.get("mail_source") or "").strip()
        if outlook:
            mail_source = "outlook"
        if mail_source == "outlook":
            method = "outlook_protocol_login"
        elif mail_source == "paymesh_card" and resources.get("paymesh_card_code"):
            method = "paymesh_card_protocol_login"
            paymesh_status = (resources.get("paymesh_session_status") or "").strip().lower()
            if paymesh_status and paymesh_status != "active":
                blockers.append(f"paymesh_session_{paymesh_status}")
        elif mail_source == "cf_temp":
            if get_setting("cf_api_url", "") and get_setting("cf_domain", "") and get_cf_admin_token():
                method = "cf_temp_protocol_login"
            else:
                blockers.append("mail_cf_not_configured")
        else:
            blockers.append("no_mail_receiver")

        if method.endswith("_protocol_login"):
            if not sms_ready:
                blockers.append("sms_not_configured")
            if not (resources.get("phone_number") or "").strip():
                warnings.append("no_saved_phone")
            if not (registered or {}).get("password"):
                warnings.append("password_missing")

    safe_resources = _safe_auth_resources(resources)
    if outlook and not safe_resources.get("mail_source"):
        safe_resources["mail_source"] = "outlook"

    return {
        "email": email,
        "auth_resources": safe_resources,
        "can_attempt_reauth": bool(method and not blockers),
        "reauth_method_hint": method,
        "blockers": blockers,
        "warnings": warnings,
        "has_registered": bool(registered),
        "has_registered_rt": bool((registered or {}).get("refresh_token")),
        "has_outlook": bool(outlook),
        "sms_available": sms_ready,
    }


def parse_paymesh_card_codes(text: str) -> list[str]:
    """解析 PayMesh 卡密池。空行和 # 注释跳过，重复卡密只保留第一次。"""
    out: list[str] = []
    seen = set()
    for raw in (text or "").splitlines():
        code = raw.strip()
        if not code or code.startswith("#"):
            continue
        key = code.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(code)
    return out


def _mask_paymesh_card(code: str) -> str:
    code = (code or "").strip()
    if len(code) <= 8:
        return "*" * len(code)
    return f"{code[:4]}...{code[-4:]}"


def _read_paymesh_card_state(con: sqlite3.Connection) -> dict:
    raw = _get_setting_con(con, PAYMESH_CARD_STATE_KEY, "")
    if not raw:
        return {}
    try:
        state = json.loads(raw)
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def _write_paymesh_card_state(con: sqlite3.Connection, state: dict) -> None:
    con.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (PAYMESH_CARD_STATE_KEY, json.dumps(state, ensure_ascii=False)),
    )


def paymesh_card_stats() -> dict:
    """按配置卡密池 + 状态 JSON 汇总，不回显卡密明文。"""
    with _lock:
        con = _conn()
        codes = parse_paymesh_card_codes(_get_setting_con(con, "paymesh_card_codes", ""))
        state = _read_paymesh_card_state(con)
    out = {"available": 0, "in_use": 0, "done": 0, "failed": 0, "total": len(codes)}
    for code in codes:
        status = (state.get(code, {}) or {}).get("status") or "available"
        if status not in out:
            status = "available"
        out[status] += 1
    return out


def paymesh_card_items(limit: int = 200) -> list[dict]:
    """返回卡密->邮箱映射。卡密只返回脱敏值，明文仅留在 settings 内部使用。"""
    with _lock:
        con = _conn()
        codes = parse_paymesh_card_codes(_get_setting_con(con, "paymesh_card_codes", ""))
        state = _read_paymesh_card_state(con)
    out = []
    for code in codes[:max(1, int(limit or 200))]:
        rec = dict(state.get(code) or {})
        out.append({
            "card": _mask_paymesh_card(code),
            "email": rec.get("email", ""),
            "status": rec.get("status", "available"),
            "session_status": rec.get("session_status", ""),
            "claimed_at": rec.get("claimed_at"),
            "finished_at": rec.get("finished_at"),
            "fail_reason": rec.get("fail_reason", ""),
        })
    return out


def claim_paymesh_card(stale_seconds: float = 4 * 3600) -> Optional[dict]:
    """原子 claim 一张 PayMesh 卡密。done/failed/stale in_use 都跳过。"""
    now = time.time()
    with _lock:
        con = _conn()
        codes = parse_paymesh_card_codes(_get_setting_con(con, "paymesh_card_codes", ""))
        state = _read_paymesh_card_state(con)
        dirty = False
        for code in codes:
            rec = dict(state.get(code) or {})
            status = rec.get("status") or "available"
            if status == "in_use":
                claimed_at = float(rec.get("claimed_at") or 0)
                if claimed_at and claimed_at < now - stale_seconds:
                    rec.update({
                        "status": "failed",
                        "finished_at": now,
                        "fail_reason": "in_use 超过 4 小时，按 PayMesh 邮箱会话过期跳过",
                    })
                    state[code] = rec
                    dirty = True
                    continue
            if status != "available":
                continue
            rec.update({
                "status": "in_use",
                "claimed_at": now,
                "updated_at": now,
                "fail_reason": "",
            })
            state[code] = rec
            _write_paymesh_card_state(con, state)
            con.commit()
            return {"code": code, **rec}
        if dirty:
            _write_paymesh_card_state(con, state)
            con.commit()
    return None


def release_paymesh_card(code: str) -> None:
    code = (code or "").strip()
    if not code:
        return
    with _lock:
        con = _conn()
        state = _read_paymesh_card_state(con)
        rec = dict(state.get(code) or {})
        rec.update({
            "status": "available",
            "claimed_at": None,
            "updated_at": time.time(),
            "fail_reason": "",
        })
        state[code] = rec
        _write_paymesh_card_state(con, state)
        con.commit()


def reset_failed_paymesh_cards() -> int:
    """Reset failed PayMesh cards to available for a manual retry."""
    with _lock:
        con = _conn()
        codes = parse_paymesh_card_codes(_get_setting_con(con, "paymesh_card_codes", ""))
        state = _read_paymesh_card_state(con)
        now = time.time()
        reset = 0
        for code in codes:
            rec = dict(state.get(code) or {})
            if rec.get("status") != "failed":
                continue
            rec.update({
                "status": "available",
                "claimed_at": None,
                "finished_at": None,
                "updated_at": now,
                "fail_reason": "",
            })
            state[code] = rec
            reset += 1
        if reset:
            _write_paymesh_card_state(con, state)
            con.commit()
        return reset


def mark_paymesh_card_done(code: str, email: str = "", session_status: str = "") -> None:
    code = (code or "").strip()
    if not code:
        return
    with _lock:
        con = _conn()
        state = _read_paymesh_card_state(con)
        rec = dict(state.get(code) or {})
        rec.update({
            "status": "done",
            "email": (email or rec.get("email") or "").strip(),
            "session_status": (session_status or rec.get("session_status") or "").strip(),
            "finished_at": time.time(),
            "fail_reason": "",
        })
        state[code] = rec
        if rec.get("email"):
            _upsert_auth_resources_con(
                con,
                rec["email"],
                mail_source="paymesh_card",
                paymesh_card_code=code,
                paymesh_session_status=rec.get("session_status", ""),
            )
        _write_paymesh_card_state(con, state)
        con.commit()


def mark_paymesh_card_failed(
    code: str,
    reason: str = "",
    email: str = "",
    session_status: str = "",
) -> None:
    code = (code or "").strip()
    if not code:
        return
    with _lock:
        con = _conn()
        state = _read_paymesh_card_state(con)
        rec = dict(state.get(code) or {})
        rec.update({
            "status": "failed",
            "email": (email or rec.get("email") or "").strip(),
            "session_status": (session_status or rec.get("session_status") or "").strip(),
            "finished_at": time.time(),
            "fail_reason": (reason or "")[:500],
        })
        state[code] = rec
        if rec.get("email"):
            _upsert_auth_resources_con(
                con,
                rec["email"],
                mail_source="paymesh_card",
                paymesh_card_code=code,
                paymesh_session_status=rec.get("session_status", ""),
            )
        _write_paymesh_card_state(con, state)
        con.commit()


def update_paymesh_card_session(code: str, email: str = "", session_status: str = "") -> None:
    code = (code or "").strip()
    if not code:
        return
    with _lock:
        con = _conn()
        state = _read_paymesh_card_state(con)
        rec = dict(state.get(code) or {})
        rec.update({
            "email": (email or rec.get("email") or "").strip(),
            "session_status": (session_status or rec.get("session_status") or "").strip(),
            "updated_at": time.time(),
        })
        state[code] = rec
        if rec.get("email"):
            _upsert_auth_resources_con(
                con,
                rec["email"],
                mail_source="paymesh_card",
                paymesh_card_code=code,
                paymesh_session_status=rec.get("session_status", ""),
            )
        _write_paymesh_card_state(con, state)
        con.commit()


def _read_k12_workspace_failures(con: sqlite3.Connection) -> dict:
    cur = con.execute("SELECT value FROM settings WHERE key=?", (K12_WORKSPACE_FAILURES_KEY,))
    row = cur.fetchone()
    if not row or not row["value"]:
        return {}
    try:
        state = json.loads(row["value"])
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def get_k12_workspace_failures() -> dict:
    with _lock:
        con = _conn()
        return _read_k12_workspace_failures(con)


def update_k12_workspace_failure(
    workspace_id: str,
    success: bool,
    failure_threshold: int,
    cooldown_seconds: int,
    now: Optional[float] = None,
) -> dict:
    workspace_id = (workspace_id or "").strip()
    if not workspace_id:
        return {}
    now = time.time() if now is None else now
    with _lock:
        con = _conn()
        state = _read_k12_workspace_failures(con)
        if success:
            state.pop(workspace_id, None)
            record = {}
        else:
            old = state.get(workspace_id, {})
            try:
                failure_count = int(old.get("failure_count", 0)) + 1
            except Exception:
                failure_count = 1
            record = {"failure_count": failure_count, "last_failed_at": now}
            if failure_count >= failure_threshold:
                record["cooldown_until"] = now + cooldown_seconds
            state[workspace_id] = record
        con.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (K12_WORKSPACE_FAILURES_KEY, json.dumps(state, ensure_ascii=False)),
        )
        con.commit()
        return record


def get_mail_config() -> dict:
    """返回邮箱来源配置（admin_token 隐藏明文）。"""
    paymesh_codes = parse_paymesh_card_codes(get_setting("paymesh_card_codes", ""))
    return {
        "mail_source":   get_setting("mail_source", "outlook"),  # outlook / cf_temp / paymesh_card
        "cf_api_url":    get_setting("cf_api_url", ""),
        "cf_admin_token": "***" if get_setting("cf_admin_token") else "",
        "cf_domain":     get_setting("cf_domain", ""),
        "paymesh_card_codes": "\n".join(paymesh_codes),
        "paymesh_card_count": len(paymesh_codes),
        "paymesh_card_stats": paymesh_card_stats(),
    }


def save_mail_config(data: dict) -> None:
    """保存邮箱配置。admin_token 传 '***' 表示不修改。"""
    if "mail_source" in data:
        src = str(data["mail_source"]).strip().lower()
        if src not in ("outlook", "cf_temp", "paymesh_card"):
            src = "outlook"
        set_setting("mail_source", src)
    if "cf_api_url" in data:
        set_setting("cf_api_url", str(data["cf_api_url"]).strip())
    if "cf_domain" in data:
        set_setting("cf_domain", str(data["cf_domain"]).strip())
    if data.get("cf_admin_token") and data["cf_admin_token"] != "***":
        set_setting("cf_admin_token", str(data["cf_admin_token"]).strip())
    if "paymesh_card_codes" in data:
        set_setting("paymesh_card_codes", "\n".join(parse_paymesh_card_codes(str(data["paymesh_card_codes"] or ""))))


def get_cf_admin_token() -> str:
    """内部用：拿明文 admin_token。"""
    return get_setting("cf_admin_token", "")


# ──────────────────────── SMS 接码配置 ────────────────────────


def get_sms_config() -> dict:
    """返回 SMS 接码配置（api_key 隐藏明文）。

    sms_enabled:        '0'/'1' 是否启用接码（命中 add-phone 时才会用）
    sms_provider:       smsbower
    sms_country:        国家代码或 ID（推荐 '52' = Thailand，OpenAI 走 SMS 的唯一稳定国家）
    sms_service:        服务代码（OpenAI = 'dr'）
    sms_max_price:      号码最高单价（SmsBower / SmsBower 用，单位平台货币；空 / -1 = 不限）
    sms_reuse_phone:    '0'/'1' 同号复用（SmsBower / SmsBower 支持，省钱）
    sms_phone_success_max: 同号最多复用几次（默认 3）
    sms_auto_country:   '0'/'1' 自动选最优国家（按价格 + 库存）
    sms_auto_min_stock: 自动选国家最低库存（默认 20）
    sms_auto_max_price: 自动选国家最高单价（默认 0 = 不限）
    """
    return {
        "sms_enabled":             get_setting("sms_enabled", "0"),
        "sms_provider":            get_setting("sms_provider", "smsbower"),
        "sms_api_key":             "***" if get_setting("sms_api_key") else "",
        "sms_country":             get_setting("sms_country", "52"),
        "sms_service":             get_setting("sms_service", "dr"),
        "sms_max_price":           get_setting("sms_max_price", ""),
        "sms_reuse_phone":         get_setting("sms_reuse_phone", "1"),
        "sms_phone_success_max":   get_setting("sms_phone_success_max", "3"),
        "sms_auto_country":        get_setting("sms_auto_country", "0"),
        "sms_strict_whitelist":    get_setting("sms_strict_whitelist", "0"),
        "sms_allowed_countries":   get_setting("sms_allowed_countries", ""),
        "sms_auto_min_stock":      get_setting("sms_auto_min_stock", "20"),
        "sms_auto_max_price":      get_setting("sms_auto_max_price", ""),
        "sms_max_phone_attempts":  get_setting("sms_max_phone_attempts", ""),
        "sms_per_phone_timeout":   get_setting("sms_per_phone_timeout", "80"),
    }


def save_sms_config(data: dict) -> None:
    """保存 SMS 配置。sms_api_key 传 '***' 表示不修改。"""
    # 校验 provider
    valid_providers = {"smsbower", "nexsms"}
    if "sms_provider" in data:
        p = str(data["sms_provider"]).strip().lower()
        if p not in valid_providers:
            p = "smsbower"
        set_setting("sms_provider", p)
    # 字符串字段直接落
    for key in (
        "sms_country", "sms_service", "sms_max_price",
        "sms_phone_success_max", "sms_auto_min_stock", "sms_auto_max_price",
        "sms_max_phone_attempts", "sms_per_phone_timeout",
        "sms_allowed_countries",
    ):
        if key in data:
            set_setting(key, str(data[key]).strip())
    # 布尔字段（前端传 '0'/'1' 或 bool）
    for key in ("sms_enabled", "sms_reuse_phone", "sms_auto_country", "sms_strict_whitelist"):
        if key in data:
            v = data[key]
            if isinstance(v, bool):
                set_setting(key, "1" if v else "0")
            else:
                s = str(v).strip().lower()
                set_setting(key, "1" if s in ("1", "true", "yes", "on") else "0")
    # API key（'***' 不修改）
    if data.get("sms_api_key") and data["sms_api_key"] != "***":
        set_setting("sms_api_key", str(data["sms_api_key"]).strip())


def get_sms_internal_config() -> dict:
    """内部用：拿明文 sms_api_key,供 sms_provider 实例化使用。"""
    return {
        "sms_enabled":             get_setting("sms_enabled", "0") in ("1", "true"),
        "sms_provider":            get_setting("sms_provider", "smsbower"),
        "sms_api_key":             get_setting("sms_api_key", ""),
        "sms_country":             get_setting("sms_country", "52"),
        "sms_service":             get_setting("sms_service", "dr"),
        "sms_max_price":           get_setting("sms_max_price", ""),
        "sms_reuse_phone":         get_setting("sms_reuse_phone", "1") in ("1", "true"),
        "sms_phone_success_max":   get_setting("sms_phone_success_max", "3"),
        "sms_auto_country":        get_setting("sms_auto_country", "0") in ("1", "true"),
        "sms_strict_whitelist":    get_setting("sms_strict_whitelist", "0") in ("1", "true"),
        "sms_allowed_countries":   get_setting("sms_allowed_countries", ""),
        "sms_auto_min_stock":      get_setting("sms_auto_min_stock", "20"),
        "sms_auto_max_price":      get_setting("sms_auto_max_price", ""),
        "sms_max_phone_attempts":  get_setting("sms_max_phone_attempts", ""),
        "sms_per_phone_timeout":   get_setting("sms_per_phone_timeout", "80"),
    }


# ──────────────────────── 自动导出配置 (CPA / SUB2API) ────────────────────────


def get_export_config() -> dict:
    """返回导出配置（敏感字段做明文/'***' 占位）。

    给前端展示用：
      cpa_mgmt_key / sub2api_api_key 已设置时返回 '***'，未设置返回 ''。
      保存时传 '***' 代表不修改。
    """
    return {
        # CPA
        "cpa_enabled":     get_setting("export_cpa_enabled", "0"),
        "cpa_url":         get_setting("export_cpa_url", ""),
        "cpa_mgmt_key":    "***" if get_setting("export_cpa_mgmt_key") else "",
        "cpa_timeout":     get_setting("export_cpa_timeout", "30"),
        # SUB2API
        "sub2api_enabled":    get_setting("export_sub2api_enabled", "0"),
        "sub2api_url":        get_setting("export_sub2api_url", ""),
        "sub2api_api_key":    "***" if get_setting("export_sub2api_api_key") else "",
        "sub2api_group_ids":  get_setting("export_sub2api_group_ids", "2"),
        "sub2api_proxy_id":   get_setting("export_sub2api_proxy_id", ""),
        "sub2api_timeout":    get_setting("export_sub2api_timeout", "30"),
    }


def save_export_config(data: dict) -> None:
    """保存导出配置。密文字段传 '***' 表示不修改。"""
    # 布尔开关
    for key_in, key_out in (
        ("cpa_enabled",     "export_cpa_enabled"),
        ("sub2api_enabled", "export_sub2api_enabled"),
    ):
        if key_in in data:
            v = data[key_in]
            if isinstance(v, bool):
                set_setting(key_out, "1" if v else "0")
            else:
                s = str(v).strip().lower()
                set_setting(key_out, "1" if s in ("1", "true", "yes", "on") else "0")
    # 字符串字段（明文）
    for key_in, key_out in (
        ("cpa_url",            "export_cpa_url"),
        ("cpa_timeout",        "export_cpa_timeout"),
        ("sub2api_url",        "export_sub2api_url"),
        ("sub2api_group_ids",  "export_sub2api_group_ids"),
        ("sub2api_proxy_id",   "export_sub2api_proxy_id"),
        ("sub2api_timeout",    "export_sub2api_timeout"),
    ):
        if key_in in data:
            set_setting(key_out, str(data[key_in] or "").strip())
    # 密文字段（'***' 不修改）
    if data.get("cpa_mgmt_key") and data["cpa_mgmt_key"] != "***":
        set_setting("export_cpa_mgmt_key", str(data["cpa_mgmt_key"]).strip())
    if data.get("sub2api_api_key") and data["sub2api_api_key"] != "***":
        set_setting("export_sub2api_api_key", str(data["sub2api_api_key"]).strip())


def get_export_internal_config() -> dict:
    """内部用：拿明文密钥 + 解析后的 enabled 布尔。供 registrar / app.test 调用。

    返回两个子配置 dict，可分别传给 exporter.export_to_cpa / export_to_sub2api。
    """
    cpa = {
        "enabled":      get_setting("export_cpa_enabled", "0") in ("1", "true"),
        "cpa_url":      get_setting("export_cpa_url", ""),
        "cpa_mgmt_key": get_setting("export_cpa_mgmt_key", ""),
        "cpa_timeout":  get_setting("export_cpa_timeout", "30"),
    }
    sub2api = {
        "enabled":            get_setting("export_sub2api_enabled", "0") in ("1", "true"),
        "sub2api_url":        get_setting("export_sub2api_url", ""),
        "sub2api_api_key":    get_setting("export_sub2api_api_key", ""),
        "sub2api_group_ids":  get_setting("export_sub2api_group_ids", "2"),
        "sub2api_proxy_id":   get_setting("export_sub2api_proxy_id", ""),
        "sub2api_timeout":    get_setting("export_sub2api_timeout", "30"),
    }
    return {"cpa": cpa, "sub2api": sub2api}


# 模块加载时自动建表
init_db()
