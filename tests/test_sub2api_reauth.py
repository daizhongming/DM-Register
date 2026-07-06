import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webui import sub2api_reauth
from webui.exporter import _b64url_json


def _jwt(payload):
    return f"{_b64url_json({'alg': 'none'})}.{_b64url_json(payload)}.sig"


def test_looks_reauth_needed_from_error_and_temp_reason():
    ok, reason = sub2api_reauth._looks_reauth_needed({
        "status": "error",
        "error_message": "OPENAI_OAUTH_TOKEN_REFRESH_FAILED: status 401",
    })
    assert ok is True
    assert reason == "401"

    ok, reason = sub2api_reauth._looks_reauth_needed({
        "temp_unschedulable_reason": '{"status_code":401,"error_message":"bad token"}',
    })
    assert ok is True
    assert reason == "temp_unschedulable status_code=401"

    ok, reason = sub2api_reauth._looks_reauth_needed({}, {"needs_reauth": True})
    assert ok is True
    assert reason == "usage.needs_reauth"


def test_looks_reauth_needed_skips_workspace_403():
    ok, reason = sub2api_reauth._looks_reauth_needed({
        "status": "error",
        "error_message": (
            'Access forbidden (403): {"message":"Unauthorized: Contact your '
            'ChatGPT workspace administrator for access.",'
            '"code":"codex_workspace_access_denied"}'
        ),
    })
    assert ok is False
    assert reason in ("403", "access forbidden", "workspace_access_denied", "codex_workspace_access_denied")


def test_email_from_account_uses_extra_then_name():
    assert sub2api_reauth._email_from_account({
        "extra": {"email": "USER@Example.COM"},
        "name": "ignored@example.com",
    }) == "user@example.com"

    assert sub2api_reauth._email_from_account({
        "name": "user@example.com [aaaaaaaa]",
    }) == "user@example.com"


def test_build_apply_credentials_preserves_old_non_sensitive_fields():
    access = _jwt({
        "exp": 4102444800,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "personal-account",
            "chatgpt_user_id": "new-user",
        },
    })
    account = {
        "credentials": {
            "chatgpt_account_id": "workspace-account",
            "workspace_id": "workspace-account",
            "model_mapping": {"gpt-5": "gpt-5"},
        }
    }
    merged = sub2api_reauth.build_apply_credentials(account, {
        "email": "user@example.com",
        "access_token": access,
        "refresh_token": "new-refresh",
        "id_token": "new-id",
    })

    assert merged["access_token"] == access
    assert merged["refresh_token"] == "new-refresh"
    assert merged["id_token"] == "new-id"
    assert merged["chatgpt_account_id"] == "workspace-account"
    assert merged["workspace_id"] == "workspace-account"
    assert merged["model_mapping"] == {"gpt-5": "gpt-5"}


def test_reauthorize_one_uses_authflow_password_not_outlook_password():
    access = _jwt({
        "exp": 4102444800,
        "https://api.openai.com/auth": {"chatgpt_account_id": "account"},
    })
    calls = {}
    saved = []
    applied = []

    class FakeMail:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            calls["mail_password"] = kwargs.get("password")

    class FakeResult:
        def to_dict(self):
            return {
                "email": "user@example.com",
                "password": "openai-login-password",
                "access_token": access,
                "refresh_token": "new-refresh",
                "id_token": "new-id",
            }

    class FakeFlow:
        def __init__(self, cfg, sms_callback=None):
            calls["proxy"] = cfg.proxy
            calls["sms_callback"] = sms_callback

        def run_protocol_login(self, mail, email, password):
            calls["login_email"] = email
            calls["login_password_arg"] = password
            return FakeResult()

    old_get_account = sub2api_reauth.db.get_account
    old_get_registered = sub2api_reauth.db.get_registered
    old_describe = sub2api_reauth.db.describe_reauth_capability
    old_save_registered = sub2api_reauth.db.save_registered
    old_upsert_auth_resources = sub2api_reauth.db.upsert_auth_resources
    old_auth_flow = sub2api_reauth.AuthFlow
    old_mail_provider = sub2api_reauth.OutlookMailProvider
    old_import_cffi = sub2api_reauth.exporter._import_cffi
    old_post = sub2api_reauth._sub2api_post
    try:
        sub2api_reauth.db.get_registered = lambda email: None
        sub2api_reauth.db.get_account = lambda email: {
            "email": email,
            "password": "outlook-password",
            "client_id": "client",
            "refresh_token": "mail-refresh",
        }
        sub2api_reauth.db.describe_reauth_capability = lambda email, ignore_registered_rt=False: {
            "can_attempt_reauth": True,
            "reauth_method_hint": "outlook_protocol_login",
            "auth_resources": {"mail_source": "outlook"},
            "blockers": [],
            "sms_available": True,
            "has_outlook": True,
        }
        sub2api_reauth.db.save_registered = lambda d: saved.append(dict(d))
        sub2api_reauth.db.upsert_auth_resources = lambda email, **fields: None
        sub2api_reauth.AuthFlow = FakeFlow
        sub2api_reauth.OutlookMailProvider = FakeMail
        sub2api_reauth.exporter._import_cffi = lambda: object()

        def fake_post(cffi, api_url, api_key, path, timeout, payload):
            applied.append({
                "api_url": api_url,
                "api_key": api_key,
                "path": path,
                "payload": payload,
            })
            return {"ok": True}

        sub2api_reauth._sub2api_post = fake_post

        result = sub2api_reauth._reauthorize_one(
            {"id": 7, "extra": {"email": "user@example.com"}, "credentials": {}},
            {"sub2api_url": "https://sub2api.test", "sub2api_api_key": "key", "sub2api_timeout": "5"},
            proxy="http://127.0.0.1:7890",
            otp_timeout=30,
            log_fn=None,
            sms_callback="sms",
        )
    finally:
        sub2api_reauth.db.get_account = old_get_account
        sub2api_reauth.db.get_registered = old_get_registered
        sub2api_reauth.db.describe_reauth_capability = old_describe
        sub2api_reauth.db.save_registered = old_save_registered
        sub2api_reauth.db.upsert_auth_resources = old_upsert_auth_resources
        sub2api_reauth.AuthFlow = old_auth_flow
        sub2api_reauth.OutlookMailProvider = old_mail_provider
        sub2api_reauth.exporter._import_cffi = old_import_cffi
        sub2api_reauth._sub2api_post = old_post

    assert result["ok"] is True
    assert calls["mail_password"] == "outlook-password"
    assert calls["login_password_arg"] == ""
    assert calls["proxy"] == "http://127.0.0.1:7890"
    assert saved[0]["password"] == "openai-login-password"
    assert applied[0]["path"] == "/admin/accounts/7/apply-oauth-credentials"
    assert applied[0]["payload"]["credentials"]["refresh_token"] == "new-refresh"


def test_reauthorize_one_uses_registered_refresh_before_outlook_login():
    calls = {}

    old_get_registered = sub2api_reauth.db.get_registered
    old_get_account = sub2api_reauth.db.get_account
    old_upsert_auth_resources = sub2api_reauth.db.upsert_auth_resources
    old_refresh = sub2api_reauth.exporter.refresh_codex_token
    old_apply = sub2api_reauth._apply_fresh_credentials
    try:
        sub2api_reauth.db.get_registered = lambda email: {
            "email": email,
            "password": "saved-password",
            "refresh_token": "old-rt",
            "id_token": "old-id",
        }
        sub2api_reauth.db.get_account = lambda email: (_ for _ in ()).throw(
            AssertionError("Outlook account should not be used when registered RT refresh works")
        )
        sub2api_reauth.db.upsert_auth_resources = lambda email, **fields: None

        def fake_refresh(rt):
            calls["refresh_token"] = rt
            return {
                "access_token": "new-access",
                "refresh_token": "new-rt",
                "id_token": "new-id",
            }

        def fake_apply(account, cfg, fresh, log_fn):
            calls["fresh"] = dict(fresh)
            return {"ok": True, "account_id": account["id"], "email": fresh["email"]}

        sub2api_reauth.exporter.refresh_codex_token = fake_refresh
        sub2api_reauth._apply_fresh_credentials = fake_apply

        result = sub2api_reauth._reauthorize_one(
            {"id": 9, "extra": {"email": "user@example.com"}, "credentials": {}},
            {"sub2api_url": "https://sub2api.test", "sub2api_api_key": "key"},
            proxy="",
            otp_timeout=30,
            log_fn=None,
            sms_callback=None,
        )
    finally:
        sub2api_reauth.db.get_registered = old_get_registered
        sub2api_reauth.db.get_account = old_get_account
        sub2api_reauth.db.upsert_auth_resources = old_upsert_auth_resources
        sub2api_reauth.exporter.refresh_codex_token = old_refresh
        sub2api_reauth._apply_fresh_credentials = old_apply

    assert result["ok"] is True
    assert result["method"] == "registered_refresh_token"
    assert calls["refresh_token"] == "old-rt"
    assert calls["fresh"]["refresh_token"] == "new-rt"


def test_reauthorize_one_falls_back_to_paymesh_after_rt_failure():
    import mail_paymesh

    access = _jwt({
        "exp": 4102444800,
        "https://api.openai.com/auth": {"chatgpt_account_id": "account"},
    })
    calls = {}

    class FakePayMesh:
        def __init__(self, **kwargs):
            calls["paymesh_kwargs"] = kwargs

    class FakeResult:
        def to_dict(self):
            return {
                "access_token": access,
                "refresh_token": "new-rt",
                "id_token": "new-id",
            }

    class FakeFlow:
        def __init__(self, cfg, sms_callback=None):
            pass

        def run_protocol_login(self, mail, email, password):
            calls["mail"] = mail
            calls["email"] = email
            calls["password"] = password
            return FakeResult()

    old_get_registered = sub2api_reauth.db.get_registered
    old_describe = sub2api_reauth.db.describe_reauth_capability
    old_get_auth_resources = sub2api_reauth.db.get_auth_resources
    old_save_registered = sub2api_reauth.db.save_registered
    old_upsert_auth_resources = sub2api_reauth.db.upsert_auth_resources
    old_refresh = sub2api_reauth.exporter.refresh_codex_token
    old_apply = sub2api_reauth._apply_fresh_credentials
    old_flow = sub2api_reauth.AuthFlow
    old_paymesh = mail_paymesh.PayMeshCardEmailProvider
    try:
        sub2api_reauth.db.get_registered = lambda email: {
            "email": email,
            "password": "saved-openai-password",
            "refresh_token": "reused-rt",
        }
        sub2api_reauth.exporter.refresh_codex_token = lambda rt: (_ for _ in ()).throw(RuntimeError("invalid_grant"))
        sub2api_reauth.db.describe_reauth_capability = lambda email, ignore_registered_rt=False: {
            "can_attempt_reauth": True,
            "reauth_method_hint": "paymesh_card_protocol_login",
            "auth_resources": {"mail_source": "paymesh_card", "paymesh_card": "CARD...OLD"},
            "blockers": [],
            "sms_available": True,
            "has_outlook": False,
        }
        sub2api_reauth.db.get_auth_resources = lambda email: {
            "mail_source": "paymesh_card",
            "paymesh_card_code": "CARD-OLD",
        }
        sub2api_reauth.db.save_registered = lambda d: None
        sub2api_reauth.db.upsert_auth_resources = lambda email, **fields: None
        sub2api_reauth._apply_fresh_credentials = lambda account, cfg, fresh, log_fn: {
            "ok": True,
            "account_id": account["id"],
            "email": fresh["email"],
        }
        sub2api_reauth.AuthFlow = FakeFlow
        mail_paymesh.PayMeshCardEmailProvider = FakePayMesh

        result = sub2api_reauth._reauthorize_one(
            {"id": 10, "extra": {"email": "user@example.com"}, "credentials": {}},
            {"sub2api_url": "https://sub2api.test", "sub2api_api_key": "key"},
            proxy="http://proxy",
            otp_timeout=30,
            log_fn=None,
            sms_callback=None,
        )
    finally:
        sub2api_reauth.db.get_registered = old_get_registered
        sub2api_reauth.db.describe_reauth_capability = old_describe
        sub2api_reauth.db.get_auth_resources = old_get_auth_resources
        sub2api_reauth.db.save_registered = old_save_registered
        sub2api_reauth.db.upsert_auth_resources = old_upsert_auth_resources
        sub2api_reauth.exporter.refresh_codex_token = old_refresh
        sub2api_reauth._apply_fresh_credentials = old_apply
        sub2api_reauth.AuthFlow = old_flow
        mail_paymesh.PayMeshCardEmailProvider = old_paymesh

    assert result["method"] == "paymesh_card_protocol_login"
    assert calls["paymesh_kwargs"]["card_code"] == "CARD-OLD"
    assert calls["paymesh_kwargs"]["email"] == "user@example.com"
    assert calls["password"] == "saved-openai-password"


def test_reauthorize_one_uses_totp_after_rt_failure():
    access = _jwt({
        "exp": 4102444800,
        "https://api.openai.com/auth": {"chatgpt_account_id": "account"},
    })
    calls = {}

    class FakeResult:
        def to_dict(self):
            return {
                "access_token": access,
                "refresh_token": "new-rt",
                "id_token": "new-id",
            }

    class FakeFlow:
        def __init__(self, cfg, sms_callback=None):
            pass

        def run_protocol_login(self, mail, email, password, totp_secret=""):
            calls["mail"] = mail
            calls["email"] = email
            calls["password"] = password
            calls["totp_secret"] = totp_secret
            return FakeResult()

    old_get_registered = sub2api_reauth.db.get_registered
    old_describe = sub2api_reauth.db.describe_reauth_capability
    old_get_auth_resources = sub2api_reauth.db.get_auth_resources
    old_save_registered = sub2api_reauth.db.save_registered
    old_upsert_auth_resources = sub2api_reauth.db.upsert_auth_resources
    old_refresh = sub2api_reauth.exporter.refresh_codex_token
    old_apply = sub2api_reauth._apply_fresh_credentials
    old_flow = sub2api_reauth.AuthFlow
    try:
        sub2api_reauth.db.get_registered = lambda email: {
            "email": email,
            "password": "fixed-password",
            "refresh_token": "bad-rt",
        }
        sub2api_reauth.exporter.refresh_codex_token = lambda rt: (_ for _ in ()).throw(RuntimeError("invalid_grant"))
        sub2api_reauth.db.describe_reauth_capability = lambda email, ignore_registered_rt=False: {
            "can_attempt_reauth": True,
            "reauth_method_hint": "openai_totp_protocol_login",
            "auth_resources": {"openai_totp_secret_set": True},
            "blockers": [],
            "sms_available": True,
            "has_outlook": False,
        }
        sub2api_reauth.db.get_auth_resources = lambda email: {
            "openai_totp_secret": "JBSWY3DPEHPK3PXP",
        }
        sub2api_reauth.db.save_registered = lambda d: None
        sub2api_reauth.db.upsert_auth_resources = lambda email, **fields: None
        sub2api_reauth._apply_fresh_credentials = lambda account, cfg, fresh, log_fn: {
            "ok": True,
            "account_id": account["id"],
            "email": fresh["email"],
        }
        sub2api_reauth.AuthFlow = FakeFlow

        result = sub2api_reauth._reauthorize_one(
            {"id": 12, "extra": {"email": "user@example.com"}, "credentials": {}},
            {"sub2api_url": "https://sub2api.test", "sub2api_api_key": "key"},
            proxy="",
            otp_timeout=30,
            log_fn=None,
            sms_callback=None,
        )
    finally:
        sub2api_reauth.db.get_registered = old_get_registered
        sub2api_reauth.db.describe_reauth_capability = old_describe
        sub2api_reauth.db.get_auth_resources = old_get_auth_resources
        sub2api_reauth.db.save_registered = old_save_registered
        sub2api_reauth.db.upsert_auth_resources = old_upsert_auth_resources
        sub2api_reauth.exporter.refresh_codex_token = old_refresh
        sub2api_reauth._apply_fresh_credentials = old_apply
        sub2api_reauth.AuthFlow = old_flow

    assert result["method"] == "openai_totp_protocol_login"
    assert isinstance(calls["mail"], sub2api_reauth.TotpOnlyMailProvider)
    assert calls["password"] == "fixed-password"
    assert calls["totp_secret"] == "JBSWY3DPEHPK3PXP"


def test_dry_run_returns_all_candidates_not_limited_by_max_accounts():
    candidates = [
        {"account": {"id": 1, "extra": {"email": "one@example.com"}}, "reason": "401"},
        {"account": {"id": 2, "extra": {"email": "two@example.com"}}, "reason": "usage.needs_reauth"},
        {"account": {"id": 3, "extra": {"email": "three@example.com"}}, "reason": "temp_unschedulable status_code=401"},
    ]

    old_cfg = sub2api_reauth._sub2api_cfg
    old_find = sub2api_reauth.find_401_accounts
    old_get_registered = sub2api_reauth.db.get_registered
    old_get_account = sub2api_reauth.db.get_account
    try:
        sub2api_reauth._sub2api_cfg = lambda: {}

        def fake_find_401_accounts(cfg, *, scan_limit, probe_usage, log_fn, cancel_check=None):
            return candidates

        sub2api_reauth.find_401_accounts = fake_find_401_accounts
        sub2api_reauth.db.get_registered = lambda email: {"email": email} if email == "one@example.com" else None
        sub2api_reauth.db.get_account = lambda email: {"email": email} if email == "two@example.com" else None

        result = sub2api_reauth.reauthorize_401_accounts({
            "dry_run": True,
            "scan_limit": 3,
            "max_accounts": 1,
        })
    finally:
        sub2api_reauth._sub2api_cfg = old_cfg
        sub2api_reauth.find_401_accounts = old_find
        sub2api_reauth.db.get_registered = old_get_registered
        sub2api_reauth.db.get_account = old_get_account

    assert result["candidates"] == 3
    assert len(result["results"]) == 3
    assert [r["account_id"] for r in result["results"]] == [1, 2, 3]
    assert result["results"][0]["has_registered"] is True
    assert result["results"][1]["has_outlook"] is True


def test_reauth_only_selected_account_ids_override_max_accounts():
    candidates = [
        {"account": {"id": 1, "extra": {"email": "one@example.com"}}, "reason": "401"},
        {"account": {"id": 2, "extra": {"email": "two@example.com"}}, "reason": "401"},
        {"account": {"id": 3, "extra": {"email": "three@example.com"}}, "reason": "401"},
    ]
    reauthed = []

    old_cfg = sub2api_reauth._sub2api_cfg
    old_find = sub2api_reauth.find_401_accounts
    old_reauth_one = sub2api_reauth._reauthorize_one
    try:
        sub2api_reauth._sub2api_cfg = lambda: {}
        sub2api_reauth.find_401_accounts = lambda cfg, *, scan_limit, probe_usage, log_fn, cancel_check=None: candidates

        def fake_reauth_one(account, cfg, *, proxy, otp_timeout, log_fn, sms_callback):
            reauthed.append(account["id"])
            return {"ok": True, "account_id": account["id"], "email": account["extra"]["email"]}

        sub2api_reauth._reauthorize_one = fake_reauth_one

        result = sub2api_reauth.reauthorize_401_accounts({
            "dry_run": False,
            "scan_limit": 3,
            "max_accounts": 1,
            "account_ids": [2, 3],
        })
    finally:
        sub2api_reauth._sub2api_cfg = old_cfg
        sub2api_reauth.find_401_accounts = old_find
        sub2api_reauth._reauthorize_one = old_reauth_one

    assert reauthed == [2, 3]
    assert result["success"] == 2
    assert [r["account_id"] for r in result["results"]] == [2, 3]


def test_reauth_cancel_before_scan_returns_cancelled():
    old_cfg = sub2api_reauth._sub2api_cfg
    old_find = sub2api_reauth.find_401_accounts
    try:
        sub2api_reauth._sub2api_cfg = lambda: {}
        sub2api_reauth.find_401_accounts = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("scan should stop before network work")
        )
        result = sub2api_reauth.reauthorize_401_accounts(
            {"dry_run": True},
            cancel_check=lambda: True,
        )
    finally:
        sub2api_reauth._sub2api_cfg = old_cfg
        sub2api_reauth.find_401_accounts = old_find

    assert result["cancelled"] is True
    assert result["candidates"] == 0
    assert result["results"] == []


def test_delete_sub2api_accounts_cleans_local_artifacts_by_email():
    deleted_paths = []
    cleaned = []
    old_cfg = sub2api_reauth._sub2api_cfg
    old_list = sub2api_reauth.list_sub2api_openai_oauth_accounts
    old_import_cffi = sub2api_reauth.exporter._import_cffi
    old_delete = sub2api_reauth._sub2api_delete
    old_cleanup = sub2api_reauth.db.delete_local_account_artifacts
    try:
        sub2api_reauth._sub2api_cfg = lambda: {
            "sub2api_url": "https://sub2api.test",
            "sub2api_api_key": "key",
            "sub2api_timeout": "5",
        }
        sub2api_reauth.list_sub2api_openai_oauth_accounts = lambda cfg, *, scan_limit, log_fn: [
            {"id": 7, "extra": {"email": "USER@example.com"}},
        ]
        sub2api_reauth.exporter._import_cffi = lambda: object()

        def fake_delete(cffi, api_url, api_key, path, timeout):
            deleted_paths.append(path)

        def fake_cleanup(email):
            cleaned.append(email)
            return {"email": email, "deleted": 3}

        sub2api_reauth._sub2api_delete = fake_delete
        sub2api_reauth.db.delete_local_account_artifacts = fake_cleanup

        result = sub2api_reauth.delete_sub2api_accounts([7])
    finally:
        sub2api_reauth._sub2api_cfg = old_cfg
        sub2api_reauth.list_sub2api_openai_oauth_accounts = old_list
        sub2api_reauth.exporter._import_cffi = old_import_cffi
        sub2api_reauth._sub2api_delete = old_delete
        sub2api_reauth.db.delete_local_account_artifacts = old_cleanup

    assert deleted_paths == ["/admin/accounts/7"]
    assert cleaned == ["user@example.com"]
    assert result["deleted"] == 1
    assert result["local_deleted"] == 1
    assert result["local_artifacts_deleted"] == 3


def test_delete_sub2api_accounts_uses_supplied_email_if_lookup_fails():
    cleaned = []
    old_cfg = sub2api_reauth._sub2api_cfg
    old_list = sub2api_reauth.list_sub2api_openai_oauth_accounts
    old_import_cffi = sub2api_reauth.exporter._import_cffi
    old_delete = sub2api_reauth._sub2api_delete
    old_cleanup = sub2api_reauth.db.delete_local_account_artifacts
    try:
        sub2api_reauth._sub2api_cfg = lambda: {
            "sub2api_url": "https://sub2api.test",
            "sub2api_api_key": "key",
        }
        sub2api_reauth.list_sub2api_openai_oauth_accounts = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("list failed")
        )
        sub2api_reauth.exporter._import_cffi = lambda: object()
        sub2api_reauth._sub2api_delete = lambda *args, **kwargs: None

        def fake_cleanup(email):
            cleaned.append(email)
            return {"email": email, "deleted": 1}

        sub2api_reauth.db.delete_local_account_artifacts = fake_cleanup

        result = sub2api_reauth.delete_sub2api_accounts(
            [7],
            account_emails={7: "USER@example.com"},
        )
    finally:
        sub2api_reauth._sub2api_cfg = old_cfg
        sub2api_reauth.list_sub2api_openai_oauth_accounts = old_list
        sub2api_reauth.exporter._import_cffi = old_import_cffi
        sub2api_reauth._sub2api_delete = old_delete
        sub2api_reauth.db.delete_local_account_artifacts = old_cleanup

    assert cleaned == ["user@example.com"]
    assert result["deleted"] == 1
    assert result["local_failed"] == 0


if __name__ == "__main__":
    test_looks_reauth_needed_from_error_and_temp_reason()
    test_looks_reauth_needed_skips_workspace_403()
    test_email_from_account_uses_extra_then_name()
    test_build_apply_credentials_preserves_old_non_sensitive_fields()
    test_reauthorize_one_uses_authflow_password_not_outlook_password()
    test_reauthorize_one_uses_registered_refresh_before_outlook_login()
    test_reauthorize_one_falls_back_to_paymesh_after_rt_failure()
    test_reauthorize_one_uses_totp_after_rt_failure()
    test_dry_run_returns_all_candidates_not_limited_by_max_accounts()
    test_reauth_only_selected_account_ids_override_max_accounts()
    test_reauth_cancel_before_scan_returns_cancelled()
    test_delete_sub2api_accounts_cleans_local_artifacts_by_email()
    test_delete_sub2api_accounts_uses_supplied_email_if_lookup_fails()
