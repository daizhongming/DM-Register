"""SUB2API 401 account re-authorization.

Keeps this narrow: detect OpenAI OAuth accounts that SUB2API already marks as
401/needs_reauth, login the matching local Outlook mailbox, then apply fresh
OAuth credentials back to SUB2API.
"""
from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from auth_flow import AuthFlow
from config import Config
from mail_outlook import OutlookMailProvider

from . import db, exporter

LogFn = Callable[[str, str], None]

class ReauthCancelled(Exception):
    pass


class TotpOnlyMailProvider:
    """MailProvider stub for password+TOTP attempts without mailbox access."""

    def create_mailbox(self) -> str:
        return ""

    def wait_for_otp(self, email: str, timeout: int = 180, issued_after: float = 0) -> str:
        raise RuntimeError(f"email OTP required for {email}; TOTP did not bypass mailbox verification")


def _check_cancel(cancel_check: Optional[Callable[[], bool]]) -> None:
    if cancel_check and cancel_check():
        raise ReauthCancelled("cancelled")


EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
REAUTH_HINTS = (
    "401",
    "unauthenticated",
    "needs_reauth",
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_reused",
    "refresh_token_invalidated",
    "app_session_terminated",
    "no refresh token",
)
NON_REAUTH_HINTS = (
    "403",
    "access forbidden",
    "workspace_access_denied",
    "codex_workspace_access_denied",
    "workspace administrator",
    "needs_verify",
    "is_banned",
)


def _log(log_fn: Optional[LogFn], msg: str, level: str = "info") -> None:
    if log_fn:
        log_fn(msg, level)


def _sub2api_cfg() -> dict:
    cfg = db.get_export_internal_config().get("sub2api", {})
    if not cfg.get("sub2api_url"):
        raise RuntimeError("SUB2API URL is not configured")
    if not cfg.get("sub2api_api_key"):
        raise RuntimeError("SUB2API API key is not configured")
    return cfg


def _sub2api_headers(api_url: str, api_key: str) -> dict:
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Referer": f"{api_url}/admin/accounts",
        "x-api-key": api_key,
    }


def _unwrap_sub2api_response(resp, label: str):
    body = ""
    try:
        body = (resp.text or "")[:400]
    except Exception:
        pass
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"{label} HTTP {resp.status_code}: {body}")
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(f"{label} returned non-JSON: {body}")
    if isinstance(data, dict) and "code" in data:
        try:
            code_ok = int(data.get("code")) == 0
        except Exception:
            code_ok = data.get("code") == 0
        if not code_ok:
            msg = data.get("message") or data.get("msg") or data.get("error") or body
            raise RuntimeError(f"{label} failed: {msg}")
        return data.get("data")
    return data


def _sub2api_get(cffi, api_url: str, api_key: str, path: str, timeout: int, params: Optional[dict] = None):
    resp = cffi.get(
        f"{api_url}/api/v1{path}",
        headers=_sub2api_headers(api_url, api_key),
        params=params or {},
        proxies=None,
        verify=False,
        timeout=timeout,
        impersonate="chrome110",
    )
    return _unwrap_sub2api_response(resp, f"GET {path}")


def _sub2api_post(cffi, api_url: str, api_key: str, path: str, timeout: int, payload: Optional[dict] = None):
    resp = cffi.post(
        f"{api_url}/api/v1{path}",
        headers=_sub2api_headers(api_url, api_key),
        json=payload or {},
        proxies=None,
        verify=False,
        timeout=timeout,
        impersonate="chrome110",
    )
    return _unwrap_sub2api_response(resp, f"POST {path}")


def _sub2api_delete(cffi, api_url: str, api_key: str, path: str, timeout: int):
    resp = cffi.delete(
        f"{api_url}/api/v1{path}",
        headers=_sub2api_headers(api_url, api_key),
        proxies=None,
        verify=False,
        timeout=timeout,
        impersonate="chrome110",
    )
    if 200 <= resp.status_code < 300 and not (resp.text or "").strip():
        return None
    try:
        return _unwrap_sub2api_response(resp, f"DELETE {path}")
    except RuntimeError as e:
        if 200 <= resp.status_code < 300 and "returned non-JSON" in str(e):
            return (resp.text or "")[:400]
        raise


def _paged_items(data) -> tuple[list[dict], int, int]:
    if not isinstance(data, dict):
        return [], 0, 1
    items = data.get("items") or []
    total = int(data.get("total") or len(items) or 0)
    pages = int(data.get("pages") or 1)
    return [x for x in items if isinstance(x, dict)], total, pages


def list_sub2api_openai_oauth_accounts(
    cfg: dict,
    *,
    scan_limit: int = 100,
    log_fn: Optional[LogFn] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> list[dict]:
    api_url = (cfg.get("sub2api_url") or "").rstrip("/")
    api_key = (cfg.get("sub2api_api_key") or "").strip()
    timeout = int(cfg.get("sub2api_timeout") or exporter.DEFAULT_TIMEOUT)
    cffi = exporter._import_cffi()
    out: list[dict] = []
    page = 1
    page_size = min(max(scan_limit, 1), 1000)

    while len(out) < scan_limit:
        _check_cancel(cancel_check)
        data = _sub2api_get(
            cffi,
            api_url,
            api_key,
            "/admin/accounts",
            timeout,
            params={
                "page": page,
                "page_size": page_size,
                "platform": "openai",
                "type": "oauth",
            },
        )
        _check_cancel(cancel_check)
        items, total, pages = _paged_items(data)
        out.extend(items)
        _log(log_fn, f"[SUB2API] scanned page {page}/{pages}, items={len(items)}, total={total}")
        if page >= pages or not items:
            break
        page += 1
    return out[:scan_limit]


def _account_text(account: dict) -> str:
    parts = [
        account.get("status"),
        account.get("error_message"),
        account.get("temp_unschedulable_reason"),
    ]
    extra = account.get("extra")
    if isinstance(extra, dict):
        parts.extend(str(v) for v in extra.values() if isinstance(v, (str, int, float)))
    return " ".join(str(x or "") for x in parts).lower()


def _temp_reason_status_is_401(account: dict) -> bool:
    raw = account.get("temp_unschedulable_reason")
    if not raw:
        return False
    try:
        data = json.loads(str(raw))
    except Exception:
        return False
    try:
        return int(data.get("status_code") or 0) == 401
    except Exception:
        return False


def _looks_reauth_needed(account: dict, usage: Optional[dict] = None) -> tuple[bool, str]:
    text = _account_text(account)
    if usage and (usage.get("is_forbidden") or usage.get("needs_verify") or usage.get("is_banned")):
        return False, str(usage.get("error_code") or usage.get("forbidden_type") or "forbidden")
    for hint in NON_REAUTH_HINTS:
        if hint in text:
            return False, hint
    if usage and usage.get("needs_reauth"):
        return True, "usage.needs_reauth"
    if usage and str(usage.get("error_code") or "").lower() in ("unauthenticated", "needs_reauth"):
        return True, f"usage.error_code={usage.get('error_code')}"
    if _temp_reason_status_is_401(account):
        return True, "temp_unschedulable status_code=401"
    for hint in REAUTH_HINTS:
        if hint in text:
            return True, hint
    return False, ""


def _usage_probe(cffi, cfg: dict, account_id: int) -> dict:
    api_url = (cfg.get("sub2api_url") or "").rstrip("/")
    api_key = (cfg.get("sub2api_api_key") or "").strip()
    timeout = int(cfg.get("sub2api_timeout") or exporter.DEFAULT_TIMEOUT)
    data = _sub2api_get(
        cffi,
        api_url,
        api_key,
        f"/admin/accounts/{account_id}/usage",
        timeout,
        params={"source": "active", "force": "true"},
    )
    return data if isinstance(data, dict) else {}


def find_401_accounts(
    cfg: dict,
    *,
    scan_limit: int = 100,
    probe_usage: bool = False,
    log_fn: Optional[LogFn] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> list[dict]:
    accounts = list_sub2api_openai_oauth_accounts(
        cfg,
        scan_limit=scan_limit,
        log_fn=log_fn,
        cancel_check=cancel_check,
    )
    results: list[tuple[bool, str]] = []
    to_probe: list[tuple[int, dict]] = []

    for idx, account in enumerate(accounts):
        _check_cancel(cancel_check)
        ok, reason = _looks_reauth_needed(account)
        results.append((ok, reason))
        if probe_usage and not ok and not reason:
            to_probe.append((idx, account))

    if to_probe:
        cffi = exporter._import_cffi()
        workers = min(8, len(to_probe))
        _log(log_fn, f"[SUB2API] usage probe parallel workers={workers}, accounts={len(to_probe)}")

        def probe(item: tuple[int, dict]) -> tuple[int, bool, str, str]:
            idx, account = item
            try:
                usage = _usage_probe(cffi, cfg, int(account.get("id")))
                ok, reason = _looks_reauth_needed(account, usage)
                return idx, ok, reason, ""
            except Exception as exc:
                return idx, False, "", str(exc)

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(probe, item) for item in to_probe]
            for future in as_completed(futures):
                _check_cancel(cancel_check)
                idx, ok, reason, err = future.result()
                done += 1
                account = accounts[idx]
                if err:
                    _log(log_fn, f"[SUB2API] usage probe failed id={account.get('id')}: {err}", "warn")
                else:
                    results[idx] = (ok, reason)
                _log(log_fn, f"[SUB2API] usage probe progress {done}/{len(to_probe)} id={account.get('id')}")

    out: list[dict] = []
    skipped = 0
    for account, (ok, reason) in zip(accounts, results):
        _check_cancel(cancel_check)
        if ok:
            out.append({"account": account, "reason": reason})
        elif reason:
            skipped += 1
            _log(
                log_fn,
                f"[SUB2API] skipped non-reauth #{account.get('id')} "
                f"{_email_from_account(account) or '-'}: {reason}",
                "warn",
            )
    _log(log_fn, f"[SUB2API] reauth candidates={len(out)} / scanned={len(accounts)} skipped={skipped}")
    return out


def _email_from_account(account: dict) -> str:
    extra = account.get("extra")
    creds = account.get("credentials")
    candidates: list[str] = []
    if isinstance(extra, dict):
        candidates.extend(str(extra.get(k) or "") for k in ("email", "mail", "username"))
    if isinstance(creds, dict):
        candidates.extend(str(creds.get(k) or "") for k in ("email", "mail", "username"))
    candidates.extend(str(account.get(k) or "") for k in ("name", "notes", "error_message"))
    for text in candidates:
        match = EMAIL_RE.search(text)
        if match:
            return match.group(0).lower()
    return ""


@contextmanager
def _patched_env(values: dict[str, str]):
    saved = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            os.environ[k] = str(v)
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def build_apply_credentials(account: dict, fresh: dict) -> dict:
    old = dict(account.get("credentials") or {})
    fresh_for_payload = dict(fresh)
    for key in (
        "chatgpt_account_id",
        "chatgpt_user_id",
        "organization_id",
        "client_id",
        "plan_type",
        "workspace_id",
    ):
        if old.get(key):
            fresh_for_payload[key] = old[key]
    payload_creds = exporter.build_sub2api_payload(fresh_for_payload, [])["credentials"]
    merged = dict(old)
    for key, value in payload_creds.items():
        if value in ("", None) and merged.get(key):
            continue
        merged[key] = value
    return merged


def _apply_fresh_credentials(account: dict, cfg: dict, fresh: dict, log_fn: Optional[LogFn]) -> dict:
    account_id = int(account.get("id"))
    email = _email_from_account(account) or str(fresh.get("email") or "").strip().lower()
    credentials = build_apply_credentials(account, fresh)
    db.save_registered({**fresh, "email": email})

    api_url = (cfg.get("sub2api_url") or "").rstrip("/")
    api_key = (cfg.get("sub2api_api_key") or "").strip()
    timeout = int(cfg.get("sub2api_timeout") or exporter.DEFAULT_TIMEOUT)
    cffi = exporter._import_cffi()
    updated = _sub2api_post(
        cffi,
        api_url,
        api_key,
        f"/admin/accounts/{account_id}/apply-oauth-credentials",
        timeout,
        {
            "type": "oauth",
            "credentials": credentials,
            "extra": {"email": email, "reauthorized_at": int(time.time())},
        },
    )
    _log(log_fn, f"[reauth] applied fresh credentials to SUB2API #{account_id} ({email})", "info")
    return {"ok": True, "account_id": account_id, "email": email, "updated": updated}


def delete_sub2api_accounts(
    account_ids: list[int],
    log_fn: Optional[LogFn] = None,
    account_emails: Optional[dict[int, str]] = None,
) -> dict:
    clean: list[int] = []
    seen: set[int] = set()
    for value in account_ids or []:
        account_id = int(value)
        if account_id <= 0 or account_id in seen:
            continue
        seen.add(account_id)
        clean.append(account_id)
    if not clean:
        raise RuntimeError("no SUB2API account ids selected")

    cfg = _sub2api_cfg()
    api_url = (cfg.get("sub2api_url") or "").rstrip("/")
    api_key = (cfg.get("sub2api_api_key") or "").strip()
    timeout = int(cfg.get("sub2api_timeout") or exporter.DEFAULT_TIMEOUT)
    cffi = exporter._import_cffi()
    results: list[dict] = []
    email_by_id: dict[int, str] = {}
    for key, value in (account_emails or {}).items():
        email = str(value or "").strip().lower()
        if not email:
            continue
        try:
            email_by_id[int(key)] = email
        except Exception:
            continue
    try:
        for account in list_sub2api_openai_oauth_accounts(cfg, scan_limit=1000, log_fn=log_fn):
            account_id = int(account.get("id") or 0)
            if account_id in seen:
                email = _email_from_account(account)
                if email:
                    email_by_id[account_id] = email
    except Exception as e:
        _log(log_fn, f"[SUB2API] account email lookup failed before delete: {e}", "warn")

    for account_id in clean:
        try:
            _sub2api_delete(cffi, api_url, api_key, f"/admin/accounts/{account_id}", timeout)
            _log(log_fn, f"[SUB2API] deleted account #{account_id}", "info")
            result = {
                "account_id": account_id,
                "ok": True,
                "remote_deleted": True,
                "email": email_by_id.get(account_id, ""),
            }
            if result["email"]:
                try:
                    result["local_cleanup"] = db.delete_local_account_artifacts(result["email"])
                    _log(log_fn, f"[local] cleaned artifacts for {result['email']}", "info")
                except Exception as e:
                    result["local_cleanup_error"] = str(e)
                    _log(log_fn, f"[local] cleanup failed for {result['email']}: {e}", "warn")
            else:
                result["local_cleanup_error"] = "email_not_found"
            results.append(result)
        except Exception as e:
            _log(log_fn, f"[SUB2API] delete failed #{account_id}: {e}", "warn")
            results.append({"account_id": account_id, "ok": False, "remote_deleted": False, "error": str(e)})

    deleted = sum(1 for r in results if r.get("ok"))
    local_failed = sum(1 for r in results if r.get("ok") and r.get("local_cleanup_error"))
    return {
        "requested": len(clean),
        "deleted": deleted,
        "failed": len(clean) - deleted,
        "local_deleted": sum(1 for r in results if r.get("local_cleanup")),
        "local_artifacts_deleted": sum((r.get("local_cleanup") or {}).get("deleted", 0) for r in results),
        "local_failed": local_failed,
        "results": results,
    }


def _try_registered_refresh(account: dict, cfg: dict, log_fn: Optional[LogFn]) -> Optional[dict]:
    email = _email_from_account(account)
    if not email:
        return None
    registered = db.get_registered(email)
    refresh_token = str((registered or {}).get("refresh_token") or "").strip()
    if not refresh_token:
        return None
    _log(log_fn, f"[reauth] refresh Codex token from local registered RT for {email}")
    fresh = exporter.refresh_codex_token(refresh_token)
    fresh.update({
        "email": email,
        "password": (registered or {}).get("password", ""),
        "refresh_token": fresh.get("refresh_token") or refresh_token,
        "id_token": fresh.get("id_token") or (registered or {}).get("id_token", ""),
    })
    return _apply_fresh_credentials(account, cfg, fresh, log_fn)


def _sms_resource_fields(ctrl) -> dict:
    if not ctrl or not getattr(ctrl, "activation", None):
        return {}
    activation = ctrl.activation
    return {
        "sms_provider": getattr(ctrl, "provider_key", ""),
        "sms_service": getattr(ctrl, "service", ""),
        "sms_country": getattr(activation, "country", "") or getattr(ctrl, "country", ""),
        "phone_number": getattr(activation, "phone_number", ""),
        "sms_activation_id": getattr(activation, "activation_id", ""),
        "sms_status": "success" if getattr(ctrl, "completed", False) else "leased",
    }


def _reauth_error_tag(error: str) -> str:
    text = (error or "").lower()
    if "password" in text or "瀵嗙爜" in text:
        return f"password_required: {error}"
    return error


def _record_reauth_resources(email: str, method: str, sms_callback=None, error: str = "") -> None:
    fields = {
        "last_reauth_method": method,
        "last_reauth_at": time.time(),
        "last_reauth_error": _reauth_error_tag(error)[:500] if error else "",
    }
    if method.startswith("outlook_"):
        fields["mail_source"] = "outlook"
    elif method.startswith("paymesh_card_"):
        fields["mail_source"] = "paymesh_card"
    elif method.startswith("cf_temp_"):
        fields["mail_source"] = "cf_temp"
    elif method.startswith("openai_totp_"):
        fields["openai_mfa_status"] = "failed" if error else "totp_login_ok"
    fields.update(_sms_resource_fields(sms_callback))
    db.upsert_auth_resources(email, **fields)


def _protocol_mail_provider(email: str, method: str, proxy: str):
    resources = db.get_auth_resources(email)
    if method == "outlook_protocol_login":
        local = db.get_account(email)
        if not local:
            raise RuntimeError(f"no local Outlook account for {email}")
        return OutlookMailProvider(
            email=local["email"],
            password=local.get("password", ""),
            client_id=local["client_id"],
            refresh_token=local["refresh_token"],
        )
    if method == "paymesh_card_protocol_login":
        from mail_paymesh import PayMeshCardEmailProvider

        code = (resources.get("paymesh_card_code") or "").strip()
        if not code:
            raise RuntimeError(f"no PayMesh card mapped for {email}")
        return PayMeshCardEmailProvider(proxy=proxy or "", card_code=code, email=email)
    if method == "openai_totp_protocol_login":
        return TotpOnlyMailProvider()
    if method == "cf_temp_protocol_login":
        from mail_cf import CFTempEmailProvider

        api_url = db.get_setting("cf_api_url", "")
        domain = db.get_setting("cf_domain", "")
        token = db.get_cf_admin_token()
        return CFTempEmailProvider(api_url=api_url, admin_token=token, domain=domain)
    raise RuntimeError(f"no protocol login method for {email}")


def _reauthorize_one(
    account: dict,
    cfg: dict,
    *,
    proxy: str,
    otp_timeout: int,
    log_fn: Optional[LogFn],
    sms_callback,
) -> dict:
    account_id = int(account.get("id"))
    email = _email_from_account(account)
    if not email:
        raise RuntimeError(f"sub2api account #{account_id} has no email in name/extra")

    try:
        refreshed = _try_registered_refresh(account, cfg, log_fn)
        if refreshed:
            refreshed["method"] = "registered_refresh_token"
            _record_reauth_resources(email, "registered_refresh_token", sms_callback)
            return refreshed
    except Exception as e:
        _log(log_fn, f"[reauth] registered RT refresh failed for {email}: {e}", "warn")

    capability = db.describe_reauth_capability(email, ignore_registered_rt=True)
    method = capability.get("reauth_method_hint") or ""
    resources = capability.get("auth_resources") or {}
    raw_resources = db.get_auth_resources(email)
    _log(
        log_fn,
        "[reauth] resources #%s %s mail=%s card=%s sms=%s ready=%s method=%s blockers=%s"
        % (
            account_id,
            email,
            "totp" if method == "openai_totp_protocol_login" else (
                resources.get("mail_source") or ("outlook" if capability.get("has_outlook") else "-")
            ),
            resources.get("paymesh_card") or "-",
            resources.get("sms_provider") or "-",
            capability.get("sms_available"),
            method or "-",
            ",".join(capability.get("blockers") or []) or "-",
        ),
    )
    if not capability.get("can_attempt_reauth"):
        msg = ",".join(capability.get("blockers") or []) or "no_reauth_path"
        _record_reauth_resources(email, method or "blocked", sms_callback, msg)
        raise RuntimeError(msg)

    _log(log_fn, f"[reauth] login {email} for SUB2API account #{account_id}")
    cfg_flow = Config()
    cfg_flow.proxy = (proxy or "").strip() or None
    mail = _protocol_mail_provider(email, method, proxy)
    flow = AuthFlow(cfg_flow, sms_callback=sms_callback)
    if sms_callback is not None and hasattr(sms_callback, "set_preferred_activation"):
        sms_callback.set_preferred_activation(
            raw_resources.get("sms_activation_id") or resources.get("sms_activation_id") or "",
            raw_resources.get("phone_number") or resources.get("phone_number") or "",
        )
    registered = db.get_registered(email) or {}
    with _patched_env({
        "OTP_TIMEOUT": str(max(30, int(otp_timeout or 180))),
        "OAUTH_CODEX_RT_EXCHANGE": "1",
        "OAUTH_CODEX_RT_BEFORE_CALLBACK": "1",
    }):
        totp_secret = (raw_resources.get("openai_totp_secret") or "").strip()
        if totp_secret:
            result = flow.run_protocol_login(mail, email, registered.get("password", ""), totp_secret=totp_secret)
        else:
            result = flow.run_protocol_login(mail, email, registered.get("password", ""))

    fresh = result.to_dict()
    fresh["email"] = email
    if not fresh.get("access_token"):
        raise RuntimeError(f"{email} reauth returned no access_token")
    if not fresh.get("refresh_token"):
        raise RuntimeError(f"{email} reauth returned no refresh_token")

    result = _apply_fresh_credentials(account, cfg, fresh, log_fn)
    result["method"] = method
    _record_reauth_resources(email, method, sms_callback)
    return result


def reauthorize_401_accounts(
    options: dict,
    *,
    log_fn: Optional[LogFn] = None,
    sms_callback_factory: Optional[Callable[[], object]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> dict:
    cfg = _sub2api_cfg()
    scan_limit = int(options.get("scan_limit") or 1000)
    max_accounts = max(1, int(options.get("max_accounts") or 5))
    probe_usage = bool(options.get("probe_usage"))
    proxy = str(options.get("proxy") or "")
    otp_timeout = int(options.get("otp_timeout") or 180)
    dry_run = bool(options.get("dry_run"))
    selected_ids = {
        int(v)
        for v in (options.get("account_ids") or [])
        if str(v).strip().isdigit() and int(v) > 0
    }

    results: list[dict] = []
    candidates: list[dict] = []

    def finish(cancelled: bool = False) -> dict:
        success = sum(1 for r in results if r.get("ok"))
        failed = 0 if dry_run else len(results) - success
        return {
            "dry_run": dry_run,
            "checked": scan_limit,
            "candidates": len(candidates),
            "success": success,
            "failed": failed,
            "cancelled": cancelled,
            "results": results,
        }

    try:
        _check_cancel(cancel_check)
        candidates = find_401_accounts(
            cfg,
            scan_limit=scan_limit,
            probe_usage=probe_usage,
            log_fn=log_fn,
            cancel_check=cancel_check,
        )
        if selected_ids:
            before = len(candidates)
            candidates = [
                item for item in candidates
                if int((item.get("account") or {}).get("id") or 0) in selected_ids
            ]
            _log(log_fn, f"[sub2api-reauth] selected={len(candidates)}/{before} ids={sorted(selected_ids)}")
        _log(log_fn, f"[sub2api-reauth] mode={'check' if dry_run else 'reauth'}")

        items = candidates if (dry_run or selected_ids) else candidates[:max_accounts]
        for item in items:
            _check_cancel(cancel_check)
            account = item["account"]
            if dry_run:
                email = _email_from_account(account)
                capability = db.describe_reauth_capability(email) if email else db.describe_reauth_capability("")
                _log(
                    log_fn,
                    f"[check] #{account.get('id')} {email or '-'} reason={item.get('reason') or '-'} "
                    f"registered_rt={capability.get('has_registered_rt')} "
                    f"outlook={capability.get('has_outlook')} "
                    f"method={capability.get('reauth_method_hint') or '-'} "
                    f"blockers={','.join(capability.get('blockers') or []) or '-'}",
                )
                results.append({
                    "ok": None,
                    "account_id": account.get("id"),
                    "email": email,
                    "reason": item.get("reason", ""),
                    "has_registered": capability.get("has_registered"),
                    "has_registered_rt": capability.get("has_registered_rt"),
                    "has_outlook": capability.get("has_outlook"),
                    "auth_resources": capability.get("auth_resources", {}),
                    "can_attempt_reauth": capability.get("can_attempt_reauth"),
                    "reauth_method_hint": capability.get("reauth_method_hint", ""),
                    "blockers": capability.get("blockers", []),
                    "warnings": capability.get("warnings", []),
                    "sms_available": capability.get("sms_available"),
                })
                continue
            sms_callback = sms_callback_factory() if sms_callback_factory else None
            try:
                result = _reauthorize_one(
                    account,
                    cfg,
                    proxy=proxy,
                    otp_timeout=otp_timeout,
                    log_fn=log_fn,
                    sms_callback=sms_callback,
                )
                result["reason"] = item.get("reason", "")
                results.append(result)
            except Exception as e:
                email = _email_from_account(account)
                msg = str(e)
                if email:
                    method = db.describe_reauth_capability(email, ignore_registered_rt=True).get("reauth_method_hint") or "failed"
                    _record_reauth_resources(email, method, sms_callback, msg)
                _log(log_fn, f"[reauth] failed #{account.get('id')} {email or '-'}: {msg}", "error")
                results.append({
                    "ok": False,
                    "account_id": account.get("id"),
                    "email": email,
                    "reason": item.get("reason", ""),
                    "error": msg,
                })
            finally:
                if sms_callback is not None and hasattr(sms_callback, "cleanup"):
                    try:
                        sms_callback.cleanup()
                    except Exception:
                        pass
        return finish()
    except ReauthCancelled:
        _log(log_fn, "[sub2api-reauth] cancelled", "warn")
        return finish(cancelled=True)
