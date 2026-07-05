import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webui import db, exporter, registrar


WS1 = "aaaaaaaa-0000-4000-8000-000000000001"
WS2 = "bbbbbbbb-0000-4000-8000-000000000002"


def _jwt(account_id: str, plan_type: str = "k12") -> str:
    return (
        f"{exporter._b64url_json({'alg': 'none'})}."
        f"{exporter._b64url_json({'exp': 4102444800, 'https://api.openai.com/auth': {'chatgpt_account_id': account_id, 'chatgpt_plan_type': plan_type}})}."
        "sig"
    )


def test_exports_each_joined_workspace_once():
    old_get_export_internal_config = db.get_export_internal_config
    old_export_to_sub2api = exporter.export_to_sub2api
    old_refresh_codex_token = exporter.refresh_codex_token
    try:
        calls = []
        refresh_calls = []
        exchange_calls = []

        class FakeResult:
            access_token = ""
            refresh_token = ""
            id_token = ""

        class FakeFlow:
            def __init__(self):
                self.result = FakeResult()

            def oauth_codex_rt_exchange(self, target_workspace_id="", **kwargs):
                exchange_calls.append(target_workspace_id)
                self.result.access_token = _jwt(target_workspace_id)
                self.result.refresh_token = f"workspace-refresh-{target_workspace_id[:4]}"
                self.result.id_token = _jwt(target_workspace_id)
                return True

        def fake_config():
            return {"sub2api": {"enabled": True, "sub2api_group_ids": "2"}}

        def fake_export(cred, cfg, log_fn=None):
            calls.append(cred)
            return {"ok": True}

        def fake_refresh(refresh_token, *args, **kwargs):
            refresh_calls.append(refresh_token)
            return {
                "access_token": _jwt("personal-account", "free"),
                "refresh_token": "new-refresh-token",
                "id_token": _jwt("personal-account", "free"),
            }

        db.get_export_internal_config = fake_config
        exporter.export_to_sub2api = fake_export
        exporter.refresh_codex_token = fake_refresh

        registrar._try_export_joined_workspaces_to_sub2api(
            "run-id",
            {
                "email": "test@example.com",
                "access_token": _jwt("personal-account", "free"),
                "refresh_token": "old-refresh-token",
                "id_token": _jwt("personal-account", "free"),
            },
            {
                "joined_workspace_ids": [WS1, WS2, WS1],
                "workspace_accounts_checked": True,
                "workspace_accounts": {
                    WS1: {
                        "id": WS1,
                        "structure": "workspace",
                        "organization_id": "org-ws1",
                        "account_user_id": "user__ws1",
                    },
                    WS2: {
                        "id": WS2,
                        "structure": "workspace",
                        "organization_id": "org-ws2",
                        "account_user_id": "user__ws2",
                    },
                },
            },
            auth_flow=FakeFlow(),
        )

        assert refresh_calls == ["old-refresh-token"]
        assert exchange_calls == [WS1, WS2]
        assert [c["refresh_token"] for c in calls] == ["workspace-refresh-aaaa", "workspace-refresh-bbbb"]
        assert [c["chatgpt_account_id"] for c in calls] == [WS1, WS2]
        assert all("chatgpt_user_id" not in c for c in calls)
        assert all("organization_id" not in c for c in calls)
        assert [c["plan_type"] for c in calls] == ["k12", "k12"]
        assert all("workspace_id" not in c for c in calls)
        assert all("workspace_account" not in c for c in calls)
        assert [c["sub2api_name"] for c in calls] == [
            "test@example.com [aaaaaaaa]",
            "test@example.com [bbbbbbbb]",
        ]
    finally:
        db.get_export_internal_config = old_get_export_internal_config
        exporter.export_to_sub2api = old_export_to_sub2api
        exporter.refresh_codex_token = old_refresh_codex_token


if __name__ == "__main__":
    test_exports_each_joined_workspace_once()
