"""K12 Workspace 自动加入模块。

注册成功后，可选地向配置的 workspace 发送加入请求。
"""
from __future__ import annotations

import json
import base64
import logging
import time
import uuid
from typing import Optional

try:
    from curl_cffi import requests
except ImportError:
    import requests

from . import db

logger = logging.getLogger(__name__)
K12_FAILURE_THRESHOLD = 3
K12_COOLDOWN_SECONDS = 24 * 60 * 60
K12_LAST_RESULT_KEY = "k12_workspace_last_result"


def _decode_jwt_payload(token: str) -> dict:
    try:
        part = str(token or "").split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        data = json.loads(base64.urlsafe_b64decode(part))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _access_token_plan_type(access_token: str) -> str:
    auth = _decode_jwt_payload(access_token).get("https://api.openai.com/auth") or {}
    if not isinstance(auth, dict):
        return ""
    return str(auth.get("chatgpt_plan_type") or auth.get("plan_type") or "").strip().lower()


def join_workspace(
    access_token: str,
    workspace_id: str,
    proxy: Optional[str] = None,
    timeout: int = 60,
    max_retries: int = 3,
) -> dict:
    """向指定 workspace 发送加入请求（request）。

    Args:
        access_token: 注册成功后的 access_token
        workspace_id: 母号的 workspace UUID
        proxy: 代理地址（可选）
        timeout: 请求超时秒数
        max_retries: 最大重试次数

    Returns:
        bool: 成功返回 True，失败返回 False
    """
    workspace_id = workspace_id.strip()
    if not workspace_id or len(workspace_id) < 32:
        logger.warning(f"无效的 workspace_id: {workspace_id}")
        return {"ok": False, "status": "invalid", "message": "invalid workspace_id"}

    url = f"https://chatgpt.com/backend-api/accounts/{workspace_id}/invites/request"
    device_id = str(uuid.uuid4())

    headers = {
        "accept": "*/*",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "en-US,en;q=0.9",
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
        "oai-device-id": device_id,
        "oai-language": "en-US",
        "origin": "https://chatgpt.com",
        "referer": "https://chatgpt.com/",
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }

    proxies = {"http": proxy, "https": proxy} if proxy else None

    # 重试机制
    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                logger.info(f"→ 重试 {attempt}/{max_retries}: {workspace_id[:8]}...")
            else:
                logger.info(f"→ POST /accounts/{workspace_id[:8]}.../invites/request")

            # 优先使用 curl_cffi（更好的 TLS 指纹）
            use_curl_cffi = False
            try:
                from curl_cffi import requests as curl_requests
                use_curl_cffi = True
            except ImportError:
                pass

            if use_curl_cffi:
                resp = curl_requests.post(
                    url,
                    headers=headers,
                    data="",
                    proxies=proxies,
                    timeout=timeout,
                    impersonate="chrome131",
                )
            else:
                resp = requests.post(
                    url,
                    headers=headers,
                    data="",
                    proxies=proxies,
                    timeout=timeout,
                )

            if resp.ok:
                logger.info(f"✓ K12 加入成功: {workspace_id[:8]}... HTTP {resp.status_code}")
                return {"ok": True, "status": resp.status_code, "message": ""}
            else:
                text = resp.text[:200] if hasattr(resp, "text") else str(resp.content[:200])
                logger.warning(f"✗ K12 加入失败: {workspace_id[:8]}... HTTP {resp.status_code}: {text}")

                # 403/404 不重试
                if resp.status_code in (403, 404):
                    return {"ok": False, "status": resp.status_code, "message": text}

                # 其他错误重试
                if attempt < max_retries:
                    import time
                    time.sleep(3)  # 增加到 3 秒
                    continue
                return {"ok": False, "status": resp.status_code, "message": text}

        except Exception as e:
            error_msg = str(e)
            if attempt < max_retries:
                logger.warning(f"✗ K12 加入请求异常: {workspace_id[:8]}... - {error_msg}，等待重试...")
                import time
                time.sleep(3)  # 增加到 3 秒
            else:
                logger.error(f"✗ K12 加入请求异常（已重试 {max_retries} 次）: {workspace_id[:8]}... - {error_msg}")
                return {"ok": False, "status": "error", "message": error_msg}

    return {"ok": False, "status": "unknown", "message": ""}


def join_workspaces_from_config(
    access_token: str,
    proxy: Optional[str] = None,
    session_token: Optional[str] = None,
    allow_personal_token: bool = False,
    workspace_ids_override: Optional[list[str]] = None,
) -> dict:
    """从数据库配置读取 workspace IDs 并批量加入。

    Args:
        access_token: 注册成功后的 access_token
        proxy: 代理地址（可选）
        session_token: session_token cookie（用于加入后重新获取 workspace AT）

    Returns:
        dict: {
            "total": int,
            "success": int,
            "failed": int,
            "workspace_access_token": str (加入成功后的 workspace AT),
            "workspace_id": str (成功加入的 workspace ID)
        }
    """
    enabled = db.get_setting("k12_enabled", "0")
    if enabled != "1":
        logger.debug("K12 加入功能未启用，跳过")
        return {"total": 0, "success": 0, "failed": 0, "attempted": 0, "skipped": 0, "joined_workspace_ids": []}

    if workspace_ids_override is None:
        workspace_ids_text = db.get_setting("k12_workspace_ids", "")
        workspace_rows = workspace_ids_text.splitlines()
    else:
        workspace_rows = workspace_ids_override
    workspace_ids = [
        str(line).strip()
        for line in workspace_rows
        if str(line).strip() and not str(line).strip().startswith("#")
    ]

    if not workspace_ids:
        logger.debug("未配置 K12 workspace IDs，跳过")
        return {"total": 0, "success": 0, "failed": 0, "attempted": 0, "skipped": 0, "joined_workspace_ids": []}

    token_plan = _access_token_plan_type(access_token)
    if token_plan in ("free", "personal") and not allow_personal_token:
        logger.warning("[k12_joiner] access_token is personal/free, skipping k12 join entirely.")
        result = {
            "total": len(workspace_ids),
            "success": 0,
            "failed": 0,
            "attempted": 0,
            "skipped": len(workspace_ids),
            "reason": "free_token",
            "joined_workspace_ids": [],
            "workspace_results": [
                {
                    "workspace_id": ws_id,
                    "ok": False,
                    "status": "skipped",
                    "message": "free_token",
                }
                for ws_id in workspace_ids
            ],
        }
        try:
            db.set_setting(K12_LAST_RESULT_KEY, json.dumps({
                **result,
                "created_at": time.time(),
            }, ensure_ascii=False))
        except Exception:
            pass
        return result

    logger.info(f"开始 K12 加入流程，共 {len(workspace_ids)} 个 workspace")

    success = 0
    failed = 0
    attempted = 0
    skipped = 0
    joined_workspace_id = None
    joined_workspace_ids: list[str] = []
    workspace_results: list[dict] = []
    failure_state = db.get_k12_workspace_failures()

    for ws_id in workspace_ids:
        now = time.time()
        record = failure_state.get(ws_id, {})
        try:
            cooldown_until = float(record.get("cooldown_until", 0))
        except Exception:
            cooldown_until = 0
        if cooldown_until > now:
            skipped += 1
            remaining = int(cooldown_until - now)
            workspace_results.append({
                "workspace_id": ws_id,
                "ok": False,
                "status": "skipped",
                "message": f"cooldown {remaining}s",
            })
            logger.info(f"K12 workspace cooldown skip: {ws_id[:8]}... remaining={remaining}s")
            continue

        attempted += 1
        join_result = join_workspace(access_token, ws_id, proxy=proxy)
        if isinstance(join_result, dict):
            ok = bool(join_result.get("ok"))
            status = join_result.get("status", "")
            message = str(join_result.get("message") or "")[:240]
        else:
            ok = bool(join_result)
            status = "ok" if ok else "failed"
            message = ""
        workspace_results.append({
            "workspace_id": ws_id,
            "ok": ok,
            "status": status,
            "message": message,
        })
        next_record = db.update_k12_workspace_failure(
            ws_id,
            ok,
            K12_FAILURE_THRESHOLD,
            K12_COOLDOWN_SECONDS,
        )
        if ok:
            success += 1
            joined_workspace_id = ws_id
            joined_workspace_ids.append(ws_id)
            failure_state.pop(ws_id, None)
        else:
            failed += 1
            failure_state[ws_id] = next_record

    result = {
        "total": len(workspace_ids),
        "success": success,
        "failed": failed,
        "attempted": attempted,
        "skipped": skipped,
        "joined_workspace_ids": joined_workspace_ids,
        "workspace_results": workspace_results,
    }
    # 如果加入成功，重新获取 session 拿到 workspace AT
    if success > 0 and session_token:
        workspace_accounts = get_workspace_accounts(session_token, proxy)
        result["workspace_accounts_checked"] = True
        result["workspace_accounts"] = {
            ws_id: workspace_accounts[ws_id]
            for ws_id in joined_workspace_ids
            if ws_id in workspace_accounts
        }
        missing = [ws_id for ws_id in joined_workspace_ids if ws_id not in workspace_accounts]
        if missing:
            logger.warning(
                "K12 joined but not visible in /backend-api/accounts, skip SUB2API export for: %s",
                ", ".join(ws_id[:8] for ws_id in missing[:10]),
            )

        logger.info("K12 加入成功，重新获取 workspace access_token...")
        workspace_data = get_workspace_access_token(session_token, proxy)
        account = (workspace_data or {}).get("account") or {}
        if workspace_data and account.get("structure") == "workspace":
            result["workspace_access_token"] = workspace_data["access_token"]
            result["workspace_account"] = account
            result["workspace_id"] = joined_workspace_id
            logger.info(f"✓ 获取到 workspace access_token (长度: {len(workspace_data['access_token'])})")
        elif workspace_data:
            logger.info(
                "Session account remains %s/%s; workspace export uses /backend-api/accounts metadata",
                account.get("structure"),
                account.get("planType"),
            )
        else:
            logger.warning("✗ 获取 workspace access_token 失败")

    logger.info(f"K12 加入完成：成功 {success}/{len(workspace_ids)}")

    logger.info(
        f"K12 join summary: success={success}/{attempted} failed={failed} "
        f"skipped={skipped}/{len(workspace_ids)}"
    )
    try:
        db.set_setting(K12_LAST_RESULT_KEY, json.dumps({
            **result,
            "created_at": time.time(),
        }, ensure_ascii=False))
    except Exception:
        pass
    return result


def _browser_get(url: str, *, headers: dict, cookies: dict, proxies: Optional[dict], timeout: int):
    try:
        from curl_cffi import requests as curl_requests
        return curl_requests.get(
            url,
            headers=headers,
            cookies=cookies,
            proxies=proxies,
            timeout=timeout,
            impersonate="chrome131",
        )
    except ImportError:
        return requests.get(
            url,
            headers=headers,
            cookies=cookies,
            proxies=proxies,
            timeout=timeout,
        )


def _chatgpt_headers() -> dict:
    return {
        "accept": "application/json, text/plain, */*",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "oai-language": "en-US",
        "origin": "https://chatgpt.com",
        "referer": "https://chatgpt.com/",
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }


def get_workspace_accounts(
    session_token: str,
    proxy: Optional[str] = None,
    timeout: int = 30,
) -> dict[str, dict]:
    """Return visible workspace accounts keyed by workspace id."""
    cookies = {"__Secure-next-auth.session-token": session_token}
    headers = _chatgpt_headers()
    proxies = {"http": proxy, "https": proxy} if proxy else None

    try:
        sess_resp = _browser_get(
            "https://chatgpt.com/api/auth/session",
            headers=headers,
            cookies=cookies,
            proxies=proxies,
            timeout=timeout,
        )
        if not sess_resp.ok:
            logger.warning(f"获取 session 失败，无法读取 workspace 列表: HTTP {sess_resp.status_code}")
            return {}
        access_token = (sess_resp.json() or {}).get("accessToken") or ""
        if not access_token:
            logger.warning("session 响应没有 accessToken，无法读取 workspace 列表")
            return {}

        auth_headers = {**headers, "authorization": f"Bearer {access_token}"}
        resp = _browser_get(
            "https://chatgpt.com/backend-api/accounts",
            headers=auth_headers,
            cookies=cookies,
            proxies=proxies,
            timeout=timeout,
        )
        if not resp.ok:
            logger.warning(f"读取 workspace 列表失败: HTTP {resp.status_code}")
            return {}

        out: dict[str, dict] = {}
        for item in (resp.json() or {}).get("items") or []:
            if not isinstance(item, dict) or item.get("structure") != "workspace":
                continue
            ws_id = str(item.get("id") or "").strip()
            if ws_id:
                account = dict(item)
                account["planType"] = str(account.get("planType") or "k12").strip() or "k12"
                out[ws_id] = account

        check_resp = _browser_get(
            "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
            headers=auth_headers,
            cookies=cookies,
            proxies=proxies,
            timeout=timeout,
        )
        if check_resp.ok:
            for ws_id, value in ((check_resp.json() or {}).get("accounts") or {}).items():
                if ws_id not in out or not isinstance(value, dict):
                    continue
                account = value.get("account") or {}
                if isinstance(account, dict):
                    out[ws_id]["organization_id"] = account.get("organization_id") or ""
                    out[ws_id]["account_user_id"] = account.get("account_user_id") or ""
        else:
            logger.warning(f"读取 workspace 详情失败: HTTP {check_resp.status_code}")

        logger.info(f"可见 workspace 数量: {len(out)}")
        return out
    except Exception as e:
        logger.warning(f"读取 workspace 列表异常: {e}")
        return {}


def get_workspace_access_token(
    session_token: str,
    proxy: Optional[str] = None,
    timeout: int = 30,
) -> Optional[dict]:
    """加入 workspace 后，重新获取 session，拿到 workspace-scoped access_token。

    Args:
        session_token: __Secure-next-auth.session-token cookie
        proxy: 代理地址（可选）
        timeout: 请求超时秒数

    Returns:
        dict: {"access_token": str, "account": dict} 包含 workspace AT 和账号信息
        None: 失败返回 None
    """
    url = "https://chatgpt.com/api/auth/session"

    cookies = {
        "__Secure-next-auth.session-token": session_token,
    }

    headers = {
        "accept": "*/*",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "oai-language": "en-US",
        "origin": "https://chatgpt.com",
        "referer": "https://chatgpt.com/",
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }

    proxies = {"http": proxy, "https": proxy} if proxy else None

    try:
        # 优先使用 curl_cffi
        use_curl_cffi = False
        try:
            from curl_cffi import requests as curl_requests
            use_curl_cffi = True
        except ImportError:
            pass

        if use_curl_cffi:
            resp = curl_requests.get(
                url,
                headers=headers,
                cookies=cookies,
                proxies=proxies,
                timeout=timeout,
                impersonate="chrome131",
            )
        else:
            resp = requests.get(
                url,
                headers=headers,
                cookies=cookies,
                proxies=proxies,
                timeout=timeout,
            )

        if resp.ok:
            data = resp.json()
            workspace_at = data.get("accessToken", "")
            if workspace_at:
                # 检查是否是 workspace account
                account = data.get("account", {})
                if account.get("structure") == "workspace":
                    logger.info(f"✓ Workspace 信息: planType={account.get('planType')}, "
                               f"workspaceId={account.get('id', '')[:20]}...")
                return {
                    "access_token": workspace_at,
                    "account": account,
                }
            else:
                logger.warning("✗ Session 响应中未找到 accessToken")
                return None
        else:
            logger.warning(f"✗ 获取 session 失败: HTTP {resp.status_code}")
            return None

    except Exception as e:
        logger.error(f"✗ 获取 workspace access_token 异常: {e}")
        return None
