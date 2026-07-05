import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webui import exporter
from webui.exporter import (
    _b64url_json,
    _parse_group_ids,
    _parse_sub2api_create_response,
    _resolve_sub2api_group_ids,
    _resolve_sub2api_proxy_id,
    build_sub2api_payload,
)


def test_parse_group_ids_rejects_names():
    assert _parse_group_ids("1,2") == [1, 2]
    assert _parse_group_ids("") == [2]
    try:
        _parse_group_ids("Dmcodex OpenAI Base")
    except ValueError:
        pass
    else:
        raise AssertionError("group names must not silently fall back")


def _jwt(payload):
    return f"{_b64url_json({'alg': 'none'})}.{_b64url_json(payload)}.sig"


def test_sub2api_payload_uses_workspace_override():
    token = _jwt({
        "exp": 4102444800,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "personal-account",
            "chatgpt_user_id": "user-1",
        },
    })
    ws_id = "00000000-0000-4000-8000-000000000123"
    payload = build_sub2api_payload({
        "email": "test@example.com",
        "access_token": token,
        "chatgpt_account_id": ws_id,
        "organization_id": "org-workspace",
        "plan_type": "k12",
        "workspace_id": ws_id,
        "sub2api_name": "test@example.com [00000000]",
    }, [9])

    assert payload["name"] == "test@example.com [00000000]"
    assert payload["credentials"]["chatgpt_account_id"] == ws_id
    assert payload["credentials"]["organization_id"] == "org-workspace"
    assert payload["credentials"]["plan_type"] == "k12"
    assert payload["credentials"]["workspace_id"] == ws_id
    assert payload["group_ids"] == [9]


def test_sub2api_payload_omits_empty_workspace_id():
    token = _jwt({
        "exp": 4102444800,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "workspace-account",
            "chatgpt_user_id": "user-1",
        },
    })
    payload = build_sub2api_payload({
        "email": "test@example.com",
        "access_token": token,
        "chatgpt_account_id": "workspace-account",
        "plan_type": "k12",
    }, [9])

    assert payload["credentials"]["chatgpt_account_id"] == "workspace-account"
    assert payload["credentials"]["plan_type"] == "k12"
    assert "workspace_id" not in payload["credentials"]


def test_resolve_sub2api_group_name():
    class Resp:
        status_code = 200

        def json(self):
            return {"data": [{"id": 9, "name": "Dmcodex OpenAI Base"}]}

    class Cffi:
        def get(self, *args, **kwargs):
            return Resp()

    assert _resolve_sub2api_group_ids(
        {"sub2api_group_ids": "Dmcodex OpenAI Base,3"},
        Cffi(),
        "https://sub2api.test",
        "key",
        30,
        lambda msg, level="info": None,
    ) == [9, 3]


def test_parse_sub2api_wrapped_response():
    class Resp:
        def __init__(self, status_code, data):
            self.status_code = status_code
            self._data = data
            self.text = str(data)

        def json(self):
            return self._data

    ok, account_id, err = _parse_sub2api_create_response(
        Resp(200, {"code": 0, "data": {"id": 123}})
    )
    assert ok is True
    assert account_id == "123"
    assert err == ""

    ok, account_id, err = _parse_sub2api_create_response(
        Resp(200, {"code": 400, "message": "invalid credentials"})
    )
    assert ok is False
    assert account_id == ""
    assert err == "invalid credentials"


def test_resolve_sub2api_proxy_name():
    class Resp:
        status_code = 200

        def json(self):
            return {"data": [{"id": 4, "name": "proxy.bestproxy.com"}]}

    class Cffi:
        def get(self, *args, **kwargs):
            return Resp()

    assert _resolve_sub2api_proxy_id(
        "proxy.bestproxy.com",
        Cffi(),
        "https://sub2api.test",
        "key",
        30,
        lambda msg, level="info": None,
    ) == 4


def test_export_to_sub2api_sends_proxy_id_from_name():
    token = _jwt({
        "exp": 4102444800,
        "https://api.openai.com/auth": {"chatgpt_account_id": "account-1"},
    })
    captured = {}

    class Resp:
        status_code = 200
        text = '{"code":0,"data":{"id":321}}'

        def json(self):
            return {"code": 0, "data": {"id": 321}}

    class Cffi:
        def get(self, *args, **kwargs):
            class ProxyResp:
                status_code = 200

                def json(self):
                    return {"data": [{"id": 4, "name": "proxy.bestproxy.com"}]}

            return ProxyResp()

        def post(self, *args, **kwargs):
            captured["payload"] = kwargs["json"]
            return Resp()

    old_import_cffi = exporter._import_cffi
    try:
        exporter._import_cffi = lambda: Cffi()
        result = exporter.export_to_sub2api(
            {"email": "test@example.com", "access_token": token, "plan_type": "plus"},
            {
                "sub2api_url": "https://sub2api.test",
                "sub2api_api_key": "key",
                "sub2api_group_ids": "9",
                "sub2api_proxy_id": "proxy.bestproxy.com",
            },
            log_fn=lambda msg, level="info": None,
        )
    finally:
        exporter._import_cffi = old_import_cffi

    assert result["ok"] is True
    assert result["account_id"] == "321"
    assert captured["payload"]["proxy_id"] == 4


def test_export_to_sub2api_skips_free_without_post():
    token = _jwt({
        "exp": 4102444800,
        "https://api.openai.com/auth": {"chatgpt_plan_type": "free"},
    })

    class Cffi:
        def post(self, *args, **kwargs):
            raise AssertionError("free account must not be posted")

    old_import_cffi = exporter._import_cffi
    try:
        exporter._import_cffi = lambda: Cffi()
        result = exporter.export_to_sub2api(
            {"email": "free@example.com", "access_token": token},
            {
                "sub2api_url": "https://sub2api.test",
                "sub2api_api_key": "key",
                "sub2api_group_ids": "9",
            },
            log_fn=lambda msg, level="info": None,
        )
    finally:
        exporter._import_cffi = old_import_cffi

    assert result["ok"] is False
    assert "free tier" in result["error"]


def test_export_to_sub2api_rejects_fake_k12_without_post():
    token = _jwt({
        "exp": 4102444800,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "personal-account",
            "chatgpt_plan_type": "free",
        },
    })

    class Cffi:
        def post(self, *args, **kwargs):
            raise AssertionError("fake k12 account must not be posted")

    old_import_cffi = exporter._import_cffi
    try:
        exporter._import_cffi = lambda: Cffi()
        result = exporter.export_to_sub2api(
            {
                "email": "fake-k12@example.com",
                "access_token": token,
                "chatgpt_account_id": "workspace-account",
                "plan_type": "k12",
            },
            {
                "sub2api_url": "https://sub2api.test",
                "sub2api_api_key": "key",
                "sub2api_group_ids": "9",
            },
            log_fn=lambda msg, level="info": None,
        )
    finally:
        exporter._import_cffi = old_import_cffi

    assert result["ok"] is False
    assert "not a workspace-scoped k12 token" in result["error"]


if __name__ == "__main__":
    test_parse_group_ids_rejects_names()
    test_sub2api_payload_uses_workspace_override()
    test_sub2api_payload_omits_empty_workspace_id()
    test_resolve_sub2api_group_name()
    test_parse_sub2api_wrapped_response()
    test_resolve_sub2api_proxy_name()
    test_export_to_sub2api_sends_proxy_id_from_name()
    test_export_to_sub2api_skips_free_without_post()
    test_export_to_sub2api_rejects_fake_k12_without_post()
