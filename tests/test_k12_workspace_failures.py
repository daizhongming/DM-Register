import json
import base64
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from webui import db, k12_joiner


WS_ID = "00000000-0000-4000-8000-000000000001"
WS_ID_2 = "00000000-0000-4000-8000-000000000002"


def _jwt(payload):
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"e30.{body}.sig"


def test_k12_skips_free_token_unless_new_account():
    old_db_path = db.DB_PATH
    old_join_workspace = k12_joiner.join_workspace
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db.DB_PATH = Path(tmp) / "webui.db"
            db.init_db()
            db.set_setting("k12_enabled", "1")
            db.set_setting("k12_workspace_ids", WS_ID)
            token = _jwt({"https://api.openai.com/auth": {"chatgpt_plan_type": "free"}})
            calls = []
            k12_joiner.join_workspace = lambda *args, **kwargs: calls.append(True) or True

            result = k12_joiner.join_workspaces_from_config(token)
            assert result["reason"] == "free_token"
            assert result["attempted"] == 0
            assert result["skipped"] == 1
            assert calls == []

            result = k12_joiner.join_workspaces_from_config(token, allow_personal_token=True)
            assert result["attempted"] == 1
            assert result["success"] == 1
            assert calls == [True]
    finally:
        db.DB_PATH = old_db_path
        k12_joiner.join_workspace = old_join_workspace


def test_k12_workspace_failure_cooldown():
    old_db_path = db.DB_PATH
    old_join_workspace = k12_joiner.join_workspace
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db.DB_PATH = Path(tmp) / "webui.db"
            db.init_db()
            db.set_setting(db.K12_WORKSPACE_FAILURES_KEY, "{broken")
            assert db.get_k12_workspace_failures() == {}

            db.set_setting("k12_enabled", "1")
            db.set_setting("k12_workspace_ids", WS_ID)

            calls = []

            def fail_join(*args, **kwargs):
                calls.append(False)
                return False

            k12_joiner.join_workspace = fail_join

            for expected_count in (1, 2, 3):
                result = k12_joiner.join_workspaces_from_config("access-token")
                assert result["attempted"] == 1
                assert result["skipped"] == 0
                assert result["failed"] == 1
                assert result["joined_workspace_ids"] == []
                assert result["workspace_results"][-1]["ok"] is False
                assert db.get_k12_workspace_failures()[WS_ID]["failure_count"] == expected_count

            state = db.get_k12_workspace_failures()
            assert state[WS_ID]["cooldown_until"] > time.time()

            result = k12_joiner.join_workspaces_from_config("access-token")
            assert result["attempted"] == 0
            assert result["skipped"] == 1
            assert result["joined_workspace_ids"] == []
            assert result["workspace_results"][0]["status"] == "skipped"
            assert len(calls) == 3

            state[WS_ID]["cooldown_until"] = time.time() - 1
            db.set_setting(db.K12_WORKSPACE_FAILURES_KEY, json.dumps(state))

            def ok_join(*args, **kwargs):
                calls.append(True)
                return True

            k12_joiner.join_workspace = ok_join
            result = k12_joiner.join_workspaces_from_config("access-token")
            assert result["success"] == 1
            assert result["attempted"] == 1
            assert result["skipped"] == 0
            assert result["joined_workspace_ids"] == [WS_ID]
            assert result["workspace_results"][0]["ok"] is True
            assert WS_ID not in db.get_k12_workspace_failures()
    finally:
        db.DB_PATH = old_db_path
        k12_joiner.join_workspace = old_join_workspace


def test_k12_workspace_override_replaces_config_list():
    old_db_path = db.DB_PATH
    old_join_workspace = k12_joiner.join_workspace
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db.DB_PATH = Path(tmp) / "webui.db"
            db.init_db()
            db.set_setting("k12_enabled", "1")
            db.set_setting("k12_workspace_ids", WS_ID)
            calls = []

            def ok_join(_access_token, workspace_id, **kwargs):
                calls.append(workspace_id)
                return True

            k12_joiner.join_workspace = ok_join
            result = k12_joiner.join_workspaces_from_config(
                "access-token",
                workspace_ids_override=[WS_ID_2],
            )
            assert result["total"] == 1
            assert result["joined_workspace_ids"] == [WS_ID_2]
            assert calls == [WS_ID_2]
    finally:
        db.DB_PATH = old_db_path
        k12_joiner.join_workspace = old_join_workspace


if __name__ == "__main__":
    test_k12_skips_free_token_unless_new_account()
    test_k12_workspace_failure_cooldown()
    test_k12_workspace_override_replaces_config_list()
    print("ok")
