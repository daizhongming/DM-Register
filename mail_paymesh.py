"""PayMesh card-code email provider.

Uses the same page flow as https://sms.paymesh.cn/:
  POST /api/v1/redeem
  GET  /api/v1/order/lookup?code=...&poll=true
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Optional

from http_client import create_http_session
from webui import db

logger = logging.getLogger(__name__)


class PayMeshCardEmailProvider:
    """邮箱卡 provider，接口兼容 AuthFlow.run_register()."""

    def __init__(
        self,
        base_url: str = "https://sms.paymesh.cn",
        proxy: str = "",
        session=None,
        card_code: str = "",
        email: str = "",
    ):
        self.base_url = (base_url or "https://sms.paymesh.cn").rstrip("/")
        self._session = session or create_http_session(proxy or None)
        self.card_code = (card_code or "").strip()
        self.email = (email or "").strip()
        self.session_status = ""
        self.codes_count = 0
        self.last_persona = None
        self._outlook_creds = None
        self.outlook_exhausted = False

    def bind_existing_card(self, card_code: str, email: str = "", session_status: str = ""):
        self.card_code = (card_code or "").strip()
        self.email = (email or self.email or "").strip()
        self.session_status = (session_status or self.session_status or "").strip()
        return self

    def _headers(self) -> dict:
        return {"accept": "application/json", "content-type": "application/json"}

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        method = method.upper()
        timeout = kwargs.pop("timeout", 15)
        headers = kwargs.pop("headers", self._headers())
        try:
            if method == "GET":
                resp = self._session.get(url, headers=headers, timeout=timeout, **kwargs)
            else:
                resp = self._session.post(url, headers=headers, timeout=timeout, **kwargs)
        except Exception as e:
            logger.warning("[paymesh] curl_cffi 请求异常，回退 urllib: %s", e)
            return self._urllib_request(method, url, headers, timeout, kwargs)
        status = int(getattr(resp, "status_code", 0) or getattr(resp, "status", 0) or 0)
        text = (getattr(resp, "text", "") or "")[:500]
        try:
            data = resp.json() if callable(getattr(resp, "json", None)) else json.loads(text or "{}")
        except Exception:
            data = {}
        if status >= 400 and not data:
            raise RuntimeError(f"PayMesh HTTP {status}: {text}")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _urllib_request(method: str, url: str, headers: dict, timeout: int, kwargs: dict) -> dict:
        params = kwargs.get("params")
        json_body = kwargs.get("json")
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        body = json.dumps(json_body, separators=(",", ":")).encode() if json_body is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                return json.loads(text or "{}")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            try:
                return json.loads(text or "{}")
            except Exception:
                raise RuntimeError(f"PayMesh HTTP {e.code}: {text[:500]}") from e

    @staticmethod
    def _api_code(body: dict) -> int:
        try:
            return int(body.get("code"))
        except Exception:
            return -1

    @staticmethod
    def _is_redeemed(body: dict) -> bool:
        if PayMeshCardEmailProvider._api_code(body) in (0, 2002, 2004):
            return True
        msg = str(body.get("msg") or body.get("message") or "")
        return any(s in msg for s in ("已使用", "使用中", "已兑换"))

    def _redeem(self, code: str) -> None:
        body = self._request("POST", "/api/v1/redeem", json={"code": code}, timeout=20)
        if not self._is_redeemed(body):
            raise RuntimeError(f"PayMesh redeem failed: {body}")

    @staticmethod
    def _is_transport_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return isinstance(exc, urllib.error.URLError) or any(s in text for s in (
            "could not resolve", "resolving timed out", "timed out", "timeout",
            "connection", "connect", "proxy", "tls", "ssl",
        ))

    def _lookup(self) -> dict:
        if not self.card_code:
            raise RuntimeError("PayMesh card code is not bound")
        body = self._request(
            "GET",
            "/api/v1/order/lookup",
            params={"code": self.card_code, "poll": "true"},
            timeout=20,
        )
        if self._api_code(body) != 0:
            raise RuntimeError(f"PayMesh lookup failed: {body}")
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def ping_platform(self) -> dict:
        """API 连通性探测。不兑换卡密，不消耗卡。"""
        body = self._request(
            "GET",
            "/api/v1/order/lookup",
            params={"code": "__codex_ping__", "poll": "true"},
            timeout=10,
        )
        if "code" not in body:
            raise RuntimeError(f"PayMesh ping returned non-API response: {body}")
        return body

    @staticmethod
    def _email_view(data: dict) -> tuple[dict, dict, list]:
        if not isinstance(data, dict) or data.get("type") != "email":
            return {}, {}, []
        email = data.get("email") if isinstance(data.get("email"), dict) else {}
        session = email.get("session") if isinstance(email.get("session"), dict) else {}
        codes = email.get("codes") or session.get("codes") or []
        return email, session, codes if isinstance(codes, list) else []

    def create_mailbox(self) -> str:
        if self.card_code and self.email:
            data = self._lookup()
            _, session, codes = self._email_view(data)
            self.session_status = (session.get("status") or self.session_status or "").strip()
            self.codes_count = len(codes)
            db.update_paymesh_card_session(self.card_code, self.email, self.session_status)
            return self.email

        claimed = db.claim_paymesh_card()
        if not claimed:
            raise RuntimeError("PayMesh 卡密池没有 available 卡密")
        self.card_code = claimed["code"]
        logger.info("[paymesh] claimed card code suffix=%s", self.card_code[-4:])
        try:
            self._redeem(self.card_code)
            data = self._lookup()
            _, session, codes = self._email_view(data)
            email = (session.get("emailAddress") or "").strip()
            if not email:
                raise RuntimeError(f"PayMesh 卡不是邮箱卡或缺 emailAddress: type={data.get('type')}")
            self.email = email
            self.session_status = (session.get("status") or "").strip()
            self.codes_count = len(codes)
            db.update_paymesh_card_session(self.card_code, email, self.session_status)
            logger.info("[paymesh] 邮箱卡兑换成功: %s status=%s", email, self.session_status or "-")
            return email
        except Exception as e:
            if self._is_transport_error(e):
                db.release_paymesh_card(self.card_code)
            else:
                db.mark_paymesh_card_failed(self.card_code, str(e), self.email, self.session_status)
            raise

    def mark_done(self) -> None:
        db.mark_paymesh_card_done(self.card_code, self.email, self.session_status)

    def mark_failed(self, reason: str = "") -> None:
        db.mark_paymesh_card_failed(self.card_code, reason, self.email, self.session_status)

    @staticmethod
    def _ts(value) -> float:
        if not value:
            return 0.0
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    @classmethod
    def _latest_code(cls, codes: list, issued_after: Optional[float]) -> str:
        def key(item: dict):
            try:
                item_id = int(item.get("id") or 0)
            except Exception:
                item_id = 0
            return (cls._ts(item.get("receivedAt")), item_id)

        for item in sorted((c for c in codes if isinstance(c, dict)), key=key, reverse=True):
            code = str(item.get("code") or "").strip()
            if not re.fullmatch(r"\d{6}", code):
                continue
            ts = cls._ts(item.get("receivedAt"))
            if issued_after and ts and ts < issued_after:
                continue
            return code
        return ""

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        timeout = max(int(timeout), 30)
        deadline = time.time() + timeout
        logger.info("[paymesh] 等待 OTP -> %s (timeout=%ss)", email_addr, timeout)
        while time.time() < deadline:
            try:
                data = self._lookup()
                _, session, codes = self._email_view(data)
                self.session_status = (session.get("status") or "").strip()
                db.update_paymesh_card_session(self.card_code, self.email, self.session_status)
                code = self._latest_code(codes, issued_after)
                if code:
                    logger.info("[paymesh] OTP=%s email=%s", code, email_addr)
                    return code
                if self.session_status and self.session_status != "active":
                    raise TimeoutError(f"PayMesh email session status={self.session_status}")
            except TimeoutError:
                raise
            except Exception as e:
                logger.warning("[paymesh] poll 异常 (吃掉重试): %s", e)
            time.sleep(6)
        raise TimeoutError(f"PayMesh OTP timeout {timeout}s for {email_addr}")
