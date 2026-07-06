"""注册 worker：调 auth_flow.run_register，并把日志/状态实时推到队列。

每个注册任务跑在独立线程；通过 `RunLogger` 把 `logging` 记录 + tail 状态推
到队列，前端用 SSE 实时收日志。
"""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]  # repo root
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from mail_outlook import OutlookMailProvider  # noqa: E402
from auth_flow import AuthFlow, RegistrationSkipError  # noqa: E402
from sms_provider import PhoneCallbackController  # noqa: E402

from . import db  # noqa: E402

# run_id -> queue of log strings; sentinel = None 表示流结束
_run_queues: dict[str, queue.Queue] = {}
_cancelled_runs: set[str] = set()
_lock = threading.Lock()

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


class QueueLogHandler(logging.Handler):
    """把 logging 记录扔进 run queue + 写 log 文件。"""

    def __init__(self, run_id: str, log_file: Path):
        super().__init__()
        self.run_id = run_id
        self._fh = open(log_file, "a", encoding="utf-8")
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            self._fh.write(msg + "\n")
            self._fh.flush()
            q = _run_queues.get(self.run_id)
            if q is not None:
                q.put(msg)
        except Exception:
            pass

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass
        super().close()


def _emit_status(run_id: str, kind: str, payload: dict | str = ""):
    """前端约定：以 `__EVENT__:` 开头的行被解析成 JSON 状态事件。"""
    import json as _json
    q = _run_queues.get(run_id)
    if q is None:
        return
    body = payload if isinstance(payload, dict) else {"message": str(payload)}
    body["kind"] = kind
    q.put("__EVENT__:" + _json.dumps(body, ensure_ascii=False))


# 网络/环境层错误特征：命中任一就把号放回 available（号本身没问题，是环境炸了）
_NETWORK_ERROR_PATTERNS = [
    "tls", "ssl", "sslerror", "connection", "connect error", "timeout", "timed out",
    "proxy", "socks", "dns", "name resolution", "name or service",
    "cloudflare", "just a moment", "403 forbidden",
    "csrf token 获取失败", "csrf token 失败",
    "/sentinel/req", "sentinel /req", "sentinel quickjs",
    "check_proxy 失败", "网络预检查",
    "curl: (35)", "curl: (28)", "curl: (6)", "curl: (7)",
    "remote disconnected", "connection reset", "connection aborted",
    "max retries exceeded",
]


def classify_error(err: str) -> str:
    """分类错误：'network'（环境/代理问题，号无辜）/ 'account'（号本身有问题）/ 'unknown'。"""
    s = (err or "").lower()
    # 先匹配 account 特征（更具体），避免子串误命中（如 "outlook OTP timeout" 含 "timeout"）
    if any(p in s for p in (
        "wrong_email_otp_code", "invalid_grant", "imap xoauth2",
        "outlook otp timeout", "paymesh otp timeout", "paymesh email session",
        "registration_disallowed",
        "已有账号", "账号被", "refresh_token 失效",
    )):
        return "account"
    if any(p in s for p in _NETWORK_ERROR_PATTERNS):
        return "network"
    return "unknown"


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


def _save_auth_resource_mapping(email: str, mail_source: str, mail=None, sms_callback=None) -> None:
    email = (email or "").strip().lower()
    if not email:
        return
    fields = {"mail_source": mail_source}
    if mail_source == "paymesh_card" and mail is not None:
        fields.update({
            "paymesh_card_code": getattr(mail, "card_code", ""),
            "paymesh_session_status": getattr(mail, "session_status", ""),
        })
    fields.update(_sms_resource_fields(sms_callback))
    db.upsert_auth_resources(email, **fields)


def _do_register(
    run_id: str,
    account: dict,
    options: dict,
    log_file: Path,
):
    """实际注册任务。

    options:
        want_access_token: bool
        want_session_token: bool
        want_refresh_token: bool
        proxy: Optional[str]
        otp_timeout: int
        allow_existing_login: bool
    """
    handler = QueueLogHandler(run_id, log_file)
    handler.setLevel(logging.INFO)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    # 第一次需要的话提到 INFO 级别
    if root_logger.level > logging.INFO or root_logger.level == 0:
        root_logger.setLevel(logging.INFO)

    email = account["email"]
    mail = None
    saved_env = {}
    # 提前读取，避免在 try 块前异常时 except 引用未定义
    mail_source = db.get_setting("mail_source", "outlook")

    try:
        # 注入环境变量（不污染全局，跑完恢复）
        env_overrides = {}
        env_overrides["OTP_TIMEOUT"] = str(int(options.get("otp_timeout") or 180))
        # 用户不要 refresh_token → 直接跳过 Codex OAuth（每次都失败浪费 ~10s + 一堆告警）
        if not options.get("want_refresh_token", True):
            env_overrides["SKIP_OAUTH_TOKEN_EXCHANGE"] = "1"
            env_overrides["OAUTH_CODEX_RT_EXCHANGE"] = "0"
            env_overrides["OAUTH_CODEX_RT_BEFORE_CALLBACK"] = "0"
        # PROXY 走 cfg.proxy，无需 env
        for k, v in env_overrides.items():
            saved_env[k] = os.environ.get(k)
            os.environ[k] = v

        cfg = Config()
        cfg.proxy = (options.get("proxy") or "").strip() or None

        # ─ 邮箱来源路由：outlook 池 vs CF Worker catch-all vs PayMesh 卡密邮箱 ─
        if mail_source == "cf_temp":
            sys_path_root = str(ROOT)
            if sys_path_root not in sys.path:
                sys.path.insert(0, sys_path_root)
            from mail_cf import CFTempEmailProvider

            api_url = db.get_setting("cf_api_url", "")
            domain  = db.get_setting("cf_domain", "")
            token   = db.get_cf_admin_token()
            if not api_url or not domain or not token:
                raise RuntimeError(
                    "CF Temp Email 未配置完整（缺 api_url / domain / admin_token），"
                    "请去「邮箱配置」Tab 填写"
                )
            mail = CFTempEmailProvider(
                api_url=api_url, admin_token=token, domain=domain,
            )
            logging.getLogger("registrar").info(
                f"[register] 邮箱来源: cf_temp / domain={domain}"
            )
        elif mail_source == "paymesh_card":
            from mail_paymesh import PayMeshCardEmailProvider

            mail = PayMeshCardEmailProvider(proxy=cfg.proxy or "")
            logging.getLogger("registrar").info(
                "[register] 邮箱来源: paymesh_card"
            )
        else:
            mail = OutlookMailProvider(
                email=account["email"],
                password=account.get("password", ""),
                client_id=account["client_id"],
                refresh_token=account["refresh_token"],
            )

        sms_callback = _build_sms_callback(run_id)
        flow = AuthFlow(cfg, sms_callback=sms_callback)
        _emit_status(run_id, "phase", {"phase": "starting", "email": email})
        logging.getLogger("registrar").info(f"[register] 开始: {email}")

        partial = False
        d: dict
        try:
            result = flow.run_register(mail)
            d = result.to_dict()
        except RegistrationSkipError as e:
            reason = str(e) or "skipped"
            logging.getLogger("registrar").info(f"[register] skipped {email}: {reason}")
            if mail_source == "paymesh_card":
                if mail is not None and hasattr(mail, "mark_failed"):
                    mail.mark_failed(f"[skipped] {reason}")
            elif mail_source != "cf_temp":
                db.mark_skipped(email, reason)
            db.finish_run(run_id, "skipped", reason, category="account")
            _emit_status(run_id, "skipped", {"message": reason, "category": "account"})
            return
        except RuntimeError as e:
            # 部分凭证也算成功（OTP 验证通过 + create_account 成功 → flow.result 有 token）
            d = flow.result.to_dict()
            need_access = options.get("want_access_token", True)
            need_session = options.get("want_session_token", True)
            need_refresh = options.get("want_refresh_token", True)
            # 用户勾选的凭证全拿到 → 算正常完成（不视为 partial）
            wanted_ok = (
                (not need_access or d.get("access_token"))
                and (not need_session or d.get("session_token"))
                and (not need_refresh or d.get("refresh_token"))
            )
            has_any = bool(
                d.get("access_token") or d.get("refresh_token") or d.get("session_token")
            )
            if wanted_ok and has_any:
                logging.getLogger("registrar").warning(
                    f"[register] 流程末段异常但用户勾选的凭证已齐: {e}"
                )
            elif has_any:
                partial = True
                logging.getLogger("registrar").warning(
                    f"[register] 部分凭证 (缺用户勾选的某项): {e}"
                )
            else:
                raise

        # ─ 用户选项过滤：未勾选的字段从结果里抹掉，DB 只存用户想要的
        full = d
        d = {
            "email": full.get("email", ""),
            "password": full.get("password", ""),
        }
        if options.get("want_access_token", True):
            d["access_token"] = full.get("access_token", "")
        if options.get("want_session_token", True):
            d["session_token"] = full.get("session_token", "")
            d["cookie_header"] = full.get("cookie_header", "")  # 同样是浏览器注入用
        if options.get("want_refresh_token", True):
            d["refresh_token"] = full.get("refresh_token", "")
            d["id_token"] = full.get("id_token", "")
        d["created_new"] = bool(full.get("created_new"))
        d["login_mode"] = bool(full.get("login_mode"))

        # 落库
        db.save_registered(d)
        _save_auth_resource_mapping(d.get("email", ""), mail_source, mail, sms_callback)
        # CF/PayMesh 模式下 account email 是虚拟占位，不操作 outlook 号池
        if mail_source == "paymesh_card":
            if hasattr(mail, "mark_done"):
                mail.mark_done()
        elif mail_source != "cf_temp":
            db.mark_done(email)

        # K12 must run before SUB2API workspace export so we know every joined workspace.
        k12_result = _try_join_k12_workspaces(run_id, d, cfg.proxy, mail_source)
        k12_enabled = db.get_setting("k12_enabled", "0") == "1"
        joined_workspace_ids = [
            str(ws_id).strip()
            for ws_id in ((k12_result or {}).get("joined_workspace_ids") or [])
            if str(ws_id).strip()
        ]

        _try_export_to_panels(run_id, d, include_sub2api=not k12_enabled)
        if joined_workspace_ids:
            _try_export_joined_workspaces_to_sub2api(
                run_id,
                d,
                k12_result or {},
                auth_flow=flow,
                mail_source=mail_source,
            )
        elif k12_enabled:
            logging.getLogger("registrar").info("[sub2api] no joined K12 workspaces to export")

        # 如果 K12 加入成功并获取到了 workspace AT，更新 d 中的 access_token
        if k12_result and k12_result.get("workspace_access_token"):
            d["access_token"] = k12_result["workspace_access_token"]
            # 把 workspace 账号信息也存到 d 里，用于导出时显示
            if k12_result.get("workspace_account"):
                d["workspace_account"] = k12_result["workspace_account"]

        result_summary = {
            "email": d.get("email"),
            "access_token_len": len(d.get("access_token") or ""),
            "session_token_len": len(d.get("session_token") or ""),
            "refresh_token_len": len(d.get("refresh_token") or ""),
            "partial": partial,
        }
        _emit_status(run_id, "done", result_summary)
        logging.getLogger("registrar").info(
            f"[register] 完成 email={d.get('email')} "
            f"at={result_summary['access_token_len']} "
            f"st={result_summary['session_token_len']} "
            f"rt={result_summary['refresh_token_len']}"
        )
        db.finish_run(run_id, "done")

    except Exception as e:
        err = str(e)
        category = classify_error(err)
        logging.getLogger("registrar").error(f"[register] 失败 (category={category}): {err}")
        logging.getLogger("registrar").error(traceback.format_exc())
        # CF 模式下不操作号池；PayMesh 卡默认失败后不复用
        if mail_source == "paymesh_card":
            if mail is not None and hasattr(mail, "mark_failed"):
                mail.mark_failed(f"[{category}] {err}")
        elif mail_source != "cf_temp":
            if category == "network":
                db.release_unused(email)
                logging.getLogger("registrar").warning(
                    f"[register] {email} 判定为网络/环境错误，号已 release 回 available"
                )
            else:
                db.mark_failed(email, f"[{category}] {err}")
        db.finish_run(run_id, "failed", err, category=category)
        _emit_status(run_id, "error", {"message": err, "category": category})

    finally:
        # 还原 env
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # 关闭 handler
        try:
            root_logger.removeHandler(handler)
            handler.close()
        except Exception:
            pass
        q = _run_queues.get(run_id)
        if q is not None:
            q.put(None)  # sentinel: 流结束


def _try_export_to_panels(
    run_id: str,
    cred: dict,
    *,
    include_cpa: bool = True,
    include_sub2api: bool = True,
) -> None:
    """注册完成后可选地把凭证导出到 CPA / SUB2API 面板。

    - 任一目标的"启用"开关关闭时,该目标跳过(不发请求);两者都未启用时整段 no-op。
    - 任何异常都不抛,只 emit 日志/状态(不影响注册主流程)。
    """
    try:
        cfg = db.get_export_internal_config()
    except Exception as e:
        logging.getLogger("registrar").warning(f"[export] 读取配置失败: {e}")
        return

    cpa_enabled = include_cpa and bool(cfg.get("cpa", {}).get("enabled"))
    sub2api_enabled = include_sub2api and bool(cfg.get("sub2api", {}).get("enabled"))
    if not (cpa_enabled or sub2api_enabled):
        return  # 用户没勾选任何目标 → 完全不执行

    from . import exporter  # 懒 import,避免未启用时强依赖

    explog = logging.getLogger("registrar")

    def _log(msg: str, level: str = "info") -> None:
        if level == "error":
            explog.error(f"[export] {msg}")
        elif level == "warn":
            explog.warning(f"[export] {msg}")
        else:
            explog.info(f"[export] {msg}")
        try:
            _emit_status(run_id, "phase", {"phase": "export", "message": msg, "level": level})
        except Exception:
            pass

    try:
        results = exporter.run_exports(
            cred,
            cpa_cfg=cfg.get("cpa") if cpa_enabled else None,
            sub2api_cfg=cfg.get("sub2api") if sub2api_enabled else None,
            log_fn=_log,
        )
    except Exception as e:
        _log(f"导出整体异常: {e}", "error")
        return

    # 汇总成一个事件给前端
    summary = {}
    if results.get("cpa") is not None:
        summary["cpa"] = {"ok": bool(results["cpa"].get("ok")),
                          "message": results["cpa"].get("message") or results["cpa"].get("error") or ""}
    if results.get("sub2api") is not None:
        summary["sub2api"] = {"ok": bool(results["sub2api"].get("ok")),
                              "message": results["sub2api"].get("message") or results["sub2api"].get("error") or ""}
    try:
        _emit_status(run_id, "phase", {"phase": "export_done", "summary": summary})
    except Exception:
        pass


def _try_export_joined_workspaces_to_sub2api(
    run_id: str,
    cred: dict,
    k12_result: dict,
    auth_flow: Optional[AuthFlow] = None,
    mail_source: str = "outlook",
) -> None:
    seen: set[str] = set()
    joined_workspace_ids: list[str] = []
    for ws_id in k12_result.get("joined_workspace_ids") or []:
        ws_id = str(ws_id).strip()
        if ws_id and ws_id not in seen:
            seen.add(ws_id)
            joined_workspace_ids.append(ws_id)
    if not joined_workspace_ids:
        return

    try:
        cfg = db.get_export_internal_config().get("sub2api", {})
    except Exception as e:
        logging.getLogger("registrar").warning(f"[sub2api] read config failed: {e}")
        return
    if not cfg.get("enabled"):
        return

    from . import exporter

    log = logging.getLogger("registrar")

    def _log(msg: str, level: str = "info") -> None:
        if level == "error":
            log.error(f"[sub2api] {msg}")
        elif level == "warn":
            log.warning(f"[sub2api] {msg}")
        else:
            log.info(f"[sub2api] {msg}")
        try:
            _emit_status(run_id, "phase", {"phase": "export", "message": msg, "level": level})
        except Exception:
            pass

    email = str(cred.get("email") or "").strip()
    ok_count = 0
    exported_workspace_ids: list[str] = []
    failed: list[str] = []
    workspace_accounts = k12_result.get("workspace_accounts") or {}
    workspace_accounts = workspace_accounts if isinstance(workspace_accounts, dict) else {}
    workspace_accounts_checked = bool(k12_result.get("workspace_accounts_checked"))
    export_cred = dict(cred)
    refresh_token = str(export_cred.get("refresh_token") or "").strip()
    if refresh_token:
        try:
            _log("[exporter] refreshing Codex token for SUB2API workspace export...", "info")
            fresh = exporter.refresh_codex_token(refresh_token)
            export_cred.update({
                "access_token": fresh["access_token"],
                "refresh_token": fresh.get("refresh_token") or refresh_token,
                "id_token": fresh.get("id_token") or export_cred.get("id_token", ""),
            })
        except Exception as e:
            _log(f"[exporter] Codex token refresh failed, using existing access_token: {e}", "warn")
    elif k12_result.get("workspace_access_token"):
        _log("[exporter] no refresh_token; using workspace access_token fallback", "warn")
        export_cred["access_token"] = k12_result["workspace_access_token"]

    def _exchange_workspace_cred(ws_id: str) -> Optional[dict]:
        if auth_flow is None or not hasattr(auth_flow, "oauth_codex_rt_exchange"):
            return None
        old_env = os.environ.get("OAUTH_CODEX_RT_ALLOW_RETRY")
        old_result = {
            "access_token": getattr(auth_flow.result, "access_token", ""),
            "refresh_token": getattr(auth_flow.result, "refresh_token", ""),
            "id_token": getattr(auth_flow.result, "id_token", ""),
        }
        try:
            os.environ["OAUTH_CODEX_RT_ALLOW_RETRY"] = "1"
            _log(f"[exporter] requesting workspace-scoped Codex token for {ws_id[:8]}...", "info")
            ok = auth_flow.oauth_codex_rt_exchange(target_workspace_id=ws_id)
            if not ok:
                return None
            candidate = {
                **cred,
                "access_token": getattr(auth_flow.result, "access_token", "") or cred.get("access_token", ""),
                "refresh_token": getattr(auth_flow.result, "refresh_token", "") or cred.get("refresh_token", ""),
                "id_token": getattr(auth_flow.result, "id_token", "") or cred.get("id_token", ""),
            }
            valid, reason = exporter.validate_k12_workspace_token(candidate, ws_id)
            if valid:
                return candidate
            _log(f"[exporter] workspace token rejected for {ws_id[:8]}: {reason}", "warn")
            return None
        except Exception as e:
            _log(f"[exporter] workspace token exchange failed for {ws_id[:8]}: {e}", "warn")
            return None
        finally:
            if old_env is None:
                os.environ.pop("OAUTH_CODEX_RT_ALLOW_RETRY", None)
            else:
                os.environ["OAUTH_CODEX_RT_ALLOW_RETRY"] = old_env
            for key, value in old_result.items():
                try:
                    setattr(auth_flow.result, key, value)
                except Exception:
                    pass

    for ws_id in joined_workspace_ids:
        workspace_meta = workspace_accounts.get(ws_id) or {}
        workspace_meta = workspace_meta if isinstance(workspace_meta, dict) else {}
        if workspace_accounts_checked and not workspace_meta:
            failed.append(f"{ws_id[:8]}: workspace not visible after join")
            continue
        workspace_export_cred = _exchange_workspace_cred(ws_id) or dict(export_cred)
        valid, reason = exporter.validate_k12_workspace_token(workspace_export_cred, ws_id)
        if not valid:
            failed.append(f"{ws_id[:8]}: token is not workspace-scoped k12 ({reason})")
            continue

        workspace_cred = {
            **workspace_export_cred,
            "account_id": ws_id,
            "chatgpt_account_id": ws_id,
            "plan_type": "k12",
            "sub2api_name": f"{email} [{ws_id[:8]}]" if email else ws_id,
        }
        # K12 uses the personal token plus chatgpt_account_id=workspace id.
        # Passing workspace org/user/workspace_id makes SUB2API classify it as free.
        for key in ("chatgpt_user_id", "organization_id", "workspace_id", "workspace_account"):
            workspace_cred.pop(key, None)
        try:
            result = exporter.export_to_sub2api(workspace_cred, cfg, log_fn=_log)
        except Exception as e:
            result = {"ok": False, "error": str(e)}
        if result.get("ok"):
            ok_count += 1
            exported_workspace_ids.append(ws_id)
        else:
            failed.append(f"{ws_id[:8]}: {result.get('error') or result.get('message') or 'unknown error'}")

    if exported_workspace_ids:
        try:
            saved = db.add_k12_usable_workspace_ids(mail_source, exported_workspace_ids)
            _log(
                f"saved {len(exported_workspace_ids)} usable workspace id(s) for {mail_source}; total {len(saved)}",
                "info",
            )
        except Exception as e:
            _log(f"save usable workspace ids failed: {e}", "warn")

    level = "info" if not failed else "warn"
    _log(
        f"workspace export done: success {ok_count}/{len(joined_workspace_ids)}, failed {len(failed)}",
        level,
    )
    try:
        _emit_status(run_id, "phase", {
            "phase": "sub2api_workspaces_done",
            "success": ok_count,
            "failed": len(failed),
            "total": len(joined_workspace_ids),
            "errors": failed[:5],
        })
    except Exception:
        pass


def _try_join_k12_workspaces(run_id: str, cred: dict, proxy: Optional[str], mail_source: str = "outlook") -> Optional[dict]:
    """注册完成后可选地加入 K12 Workspace。

    - 开关关闭时跳过（不发请求）
    - 任何异常都不抛，只 emit 日志/状态（不影响注册主流程）
    - 加入成功后重新获取 workspace access_token 并更新数据库

    Returns:
        dict: K12 加入结果，包含 workspace_access_token（如果成功）
        None: 未启用或失败
    """
    try:
        from . import k12_joiner
    except ImportError:
        logging.getLogger("registrar").warning("[k12] 导入 k12_joiner 失败，跳过")
        return None

    enabled = db.get_setting("k12_enabled", "0")
    if enabled != "1":
        return None  # 用户没勾选 → 完全不执行

    access_token = cred.get("access_token", "")
    session_token = cred.get("session_token", "")
    if not access_token:
        logging.getLogger("registrar").warning("[k12] 未获取到 access_token，跳过 K12 加入")
        return None

    if not session_token:
        logging.getLogger("registrar").warning("[k12] 未获取到 session_token，无法获取 workspace AT")

    k12log = logging.getLogger("registrar")

    def _log(msg: str, level: str = "info") -> None:
        if level == "error":
            k12log.error(f"[k12] {msg}")
        elif level == "warn":
            k12log.warning(f"[k12] {msg}")
        else:
            k12log.info(f"[k12] {msg}")
        try:
            _emit_status(run_id, "phase", {"phase": "k12", "message": msg, "level": level})
        except Exception:
            pass

    try:
        _log("开始 K12 Workspace 加入流程")
        usable_workspace_ids = db.get_k12_usable_workspace_ids(mail_source)
        if usable_workspace_ids:
            _log(f"using {len(usable_workspace_ids)} saved usable workspace id(s) for {mail_source}")
        result = k12_joiner.join_workspaces_from_config(
            access_token,
            proxy=proxy,
            session_token=session_token,
            allow_personal_token=bool(cred.get("created_new") or cred.get("login_mode")),
            workspace_ids_override=usable_workspace_ids or None,
        )

        if result["total"] > 0:
            msg = f"K12 加入完成: 成功 {result['success']}/{result['total']}"
            attempted = result.get("attempted", result["total"])
            skipped = result.get("skipped", 0)
            msg = (
                f"K12 join done: success {result['success']}/{attempted}, "
                f"failed {result['failed']}, skipped {skipped}/{result['total']}"
            )
            _log(msg, "info" if result["failed"] == 0 else "warn")

            # 如果获取到了 workspace AT，更新数据库
            if result.get("workspace_access_token"):
                email = cred.get("email", "")
                workspace_at = result["workspace_access_token"]
                workspace_id = result.get("workspace_id", "")

                _log(f"获取到 workspace access_token (长度: {len(workspace_at)})")
                _log(f"workspace ID: {workspace_id[:20]}...")

                # 更新数据库中的 access_token 为 workspace AT
                try:
                    db.update_registered_access_token(email, workspace_at)
                    _log("✓ 已更新数据库 access_token 为 workspace AT")
                except Exception as e:
                    _log(f"更新数据库失败: {e}", "warn")

            _emit_status(run_id, "phase", {
                "phase": "k12_done",
                "total": result["total"],
                "success": result["success"],
                "failed": result["failed"],
                "attempted": attempted,
                "skipped": skipped,
                "has_workspace_at": bool(result.get("workspace_access_token")),
            })

            return result  # 返回结果，包含 workspace_access_token
        else:
            _log("未配置 K12 workspace IDs", "info")
            return None

    except Exception as e:
        _log(f"K12 加入异常: {e}", "error")
        return None


def _build_sms_callback(run_id: str) -> Optional[PhoneCallbackController]:
    """根据 webui 配置创建 SMS 接码 controller。

    未启用接码或未配置 API key 时返回 None，flow 会回退到环境变量路径。
    log_fn 把租号/等码的状态推到 SSE 流，前端可见。
    """
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_enabled"):
        return None
    api_key = (cfg.get("sms_api_key") or "").strip()
    if not api_key:
        logging.getLogger("registrar").warning("[sms] 已启用接码但未配置 sms_api_key，跳过")
        return None

    smslog = logging.getLogger("registrar")

    def _log(msg: str) -> None:
        # 既写日志、又通过 _emit_status 推 phase 事件给前端
        smslog.info(f"[sms] {msg}")
        try:
            _emit_status(run_id, "phase", {"phase": "sms", "message": msg})
        except Exception:
            pass

    try:
        return PhoneCallbackController(
            provider_key=cfg["sms_provider"],
            config=cfg,
            service=cfg.get("sms_service") or "openai",
            country=cfg.get("sms_country") or "52",
            log_fn=_log,
            auto_select_country=bool(cfg.get("sms_auto_country")),
        )
    except Exception as e:
        smslog.warning(f"[sms] 创建接码 controller 失败: {e}")
        return None


def _do_sub2api_reauth(run_id: str, options: dict, log_file: Path):
    handler = QueueLogHandler(run_id, log_file)
    handler.setLevel(logging.INFO)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    if root_logger.level > logging.INFO or root_logger.level == 0:
        root_logger.setLevel(logging.INFO)

    try:
        from . import sub2api_reauth

        log = logging.getLogger("registrar")

        def _log(msg: str, level: str = "info") -> None:
            if level == "error":
                log.error(msg)
            elif level in ("warn", "warning"):
                log.warning(msg)
            else:
                log.info(msg)
            try:
                _emit_status(run_id, "phase", {
                    "phase": "sub2api_reauth",
                    "message": msg,
                    "level": level,
                })
            except Exception:
                pass

        _emit_status(run_id, "phase", {"phase": "sub2api_reauth", "message": "starting"})
        result = sub2api_reauth.reauthorize_401_accounts(
            options,
            log_fn=_log,
            sms_callback_factory=lambda: _build_sms_callback(run_id),
            cancel_check=lambda: is_run_cancelled(run_id),
        )
        logging.getLogger("registrar").info(
            "[sub2api-reauth] done success=%s failed=%s candidates=%s",
            result.get("success"), result.get("failed"), result.get("candidates"),
        )
        _emit_status(run_id, "done", {"task": "sub2api_reauth", **result})
        db.finish_run(run_id, "cancelled" if result.get("cancelled") else "done")
    except Exception as e:
        err = str(e)
        logging.getLogger("registrar").error(f"[sub2api-reauth] failed: {err}")
        logging.getLogger("registrar").error(traceback.format_exc())
        db.finish_run(run_id, "failed", err, category="unknown")
        _emit_status(run_id, "error", {"message": err, "category": "unknown"})
    finally:
        try:
            root_logger.removeHandler(handler)
            handler.close()
        except Exception:
            pass
        q = _run_queues.get(run_id)
        if q is not None:
            q.put(None)
        with _lock:
            _cancelled_runs.discard(run_id)


def start_registration(account: dict, options: dict) -> str:
    """启动一次注册任务，返回 run_id。"""
    run_id = uuid.uuid4().hex[:12]
    log_file = LOG_DIR / f"{run_id}.log"
    db.create_run(run_id, account["email"], str(log_file))

    q: queue.Queue = queue.Queue()
    with _lock:
        _run_queues[run_id] = q

    th = threading.Thread(
        target=_do_register,
        args=(run_id, account, options, log_file),
        daemon=True,
        name=f"register-{run_id}",
    )
    th.start()
    return run_id


def start_sub2api_reauth(options: dict) -> str:
    run_id = uuid.uuid4().hex[:12]
    log_file = LOG_DIR / f"{run_id}.log"
    db.create_run(run_id, "sub2api-reauth@local", str(log_file))

    q: queue.Queue = queue.Queue()
    with _lock:
        _cancelled_runs.discard(run_id)
        _run_queues[run_id] = q

    th = threading.Thread(
        target=_do_sub2api_reauth,
        args=(run_id, options, log_file),
        daemon=True,
        name=f"sub2api-reauth-{run_id}",
    )
    th.start()
    return run_id


def get_run_queue(run_id: str) -> Optional[queue.Queue]:
    return _run_queues.get(run_id)


def cancel_run(run_id: str) -> bool:
    run_id = (run_id or "").strip()
    if not run_id:
        return False
    with _lock:
        if run_id not in _run_queues:
            return False
        _cancelled_runs.add(run_id)
        q = _run_queues.get(run_id)
    if q is not None:
        q.put("__EVENT__:{\"kind\":\"phase\",\"phase\":\"sub2api_reauth\",\"message\":\"cancel requested\",\"level\":\"warn\"}")
    return True


def is_run_cancelled(run_id: str) -> bool:
    with _lock:
        return run_id in _cancelled_runs


def remove_run_queue(run_id: str) -> None:
    with _lock:
        _run_queues.pop(run_id, None)
