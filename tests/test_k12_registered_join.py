import queue
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from webui import db, registrar


WS_NEW = "cccccccc-0000-4000-8000-000000000003"


def test_registered_k12_join_uses_current_config_and_mail_source():
    old_db_path = db.DB_PATH
    old_refresh = registrar._refresh_cred_from_existing
    old_join = registrar._try_join_k12_workspaces
    old_export = registrar._try_export_joined_workspaces_to_sub2api
    run_id = "k12test"
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db.DB_PATH = Path(tmp) / "webui.db"
            db.init_db()
            db.set_setting("k12_enabled", "1")
            db.set_setting("k12_workspace_ids", WS_NEW)
            db.add_k12_usable_workspace_ids("paymesh_card", ["old-workspace"])
            db.save_registered({
                "email": "old@example.com",
                "password": "pw",
                "access_token": "access-token",
                "session_token": "session-token",
                "refresh_token": "refresh-token",
            })
            db.upsert_auth_resources("old@example.com", mail_source="paymesh_card")

            joins = []
            exports = []
            registrar._refresh_cred_from_existing = lambda cred, proxy: None

            def fake_join(run_id, cred, proxy, mail_source="outlook", **kwargs):
                joins.append((mail_source, kwargs))
                return {
                    "total": 1,
                    "success": 1,
                    "failed": 0,
                    "skipped": 0,
                    "joined_workspace_ids": [WS_NEW],
                }

            def fake_export(run_id, cred, result, auth_flow=None, mail_source="outlook"):
                exports.append((mail_source, result["joined_workspace_ids"]))

            registrar._try_join_k12_workspaces = fake_join
            registrar._try_export_joined_workspaces_to_sub2api = fake_export
            registrar._run_queues[run_id] = queue.Queue()
            db.create_run(run_id, "k12-test@local", str(Path(tmp) / "run.log"))

            registrar._do_k12_registered_join(run_id, {"all": True}, Path(tmp) / "run.log")

            assert joins == [("paymesh_card", {"use_saved_usable": False, "force_allow_personal": True})]
            assert exports == [("paymesh_card", [WS_NEW])]
    finally:
        db.DB_PATH = old_db_path
        registrar._refresh_cred_from_existing = old_refresh
        registrar._try_join_k12_workspaces = old_join
        registrar._try_export_joined_workspaces_to_sub2api = old_export
        registrar._run_queues.pop(run_id, None)


def test_registered_k12_join_runs_accounts_concurrently():
    old_db_path = db.DB_PATH
    old_refresh = registrar._refresh_cred_from_existing
    old_join = registrar._try_join_k12_workspaces
    run_id = "k12concurrency"
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db.DB_PATH = Path(tmp) / "webui.db"
            db.init_db()
            db.set_setting("k12_enabled", "1")
            db.set_setting("k12_workspace_ids", WS_NEW)
            for i in range(4):
                db.save_registered({
                    "email": f"old{i}@example.com",
                    "access_token": "access-token",
                    "session_token": "session-token",
                })

            lock = threading.Lock()
            active = 0
            max_active = 0
            registrar._refresh_cred_from_existing = lambda cred, proxy: None

            def fake_join(*args, **kwargs):
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.1)
                with lock:
                    active -= 1
                return {"failed": 0, "skipped": 0, "joined_workspace_ids": []}

            registrar._try_join_k12_workspaces = fake_join
            registrar._run_queues[run_id] = queue.Queue()
            db.create_run(run_id, "k12-test@local", str(Path(tmp) / "run.log"))

            registrar._do_k12_registered_join(
                run_id,
                {"all": True, "concurrency": 3},
                Path(tmp) / "run.log",
            )

            assert max_active >= 2
    finally:
        db.DB_PATH = old_db_path
        registrar._refresh_cred_from_existing = old_refresh
        registrar._try_join_k12_workspaces = old_join
        registrar._run_queues.pop(run_id, None)


def test_registered_k12_join_counts_cooldown_as_skipped():
    old_db_path = db.DB_PATH
    old_refresh = registrar._refresh_cred_from_existing
    old_join = registrar._try_join_k12_workspaces
    run_id = "k12cooldown"
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db.DB_PATH = Path(tmp) / "webui.db"
            db.init_db()
            db.set_setting("k12_enabled", "1")
            db.set_setting("k12_workspace_ids", WS_NEW)
            db.save_registered({
                "email": "old@example.com",
                "access_token": "access-token",
                "session_token": "session-token",
            })

            registrar._refresh_cred_from_existing = lambda cred, proxy: None
            registrar._try_join_k12_workspaces = lambda *a, **kw: {
                "failed": 0,
                "attempted": 0,
                "skipped": 1,
                "joined_workspace_ids": [],
            }
            q = queue.Queue()
            registrar._run_queues[run_id] = q
            db.create_run(run_id, "k12-test@local", str(Path(tmp) / "run.log"))

            registrar._do_k12_registered_join(run_id, {"all": True}, Path(tmp) / "run.log")

            done = None
            while not q.empty():
                item = q.get()
                if isinstance(item, str) and item.startswith("__EVENT__:"):
                    event = json.loads(item.removeprefix("__EVENT__:"))
                    if event.get("kind") == "done":
                        done = event
            assert done["accounts_failed"] == 0
            assert done["accounts_skipped"] == 1
    finally:
        db.DB_PATH = old_db_path
        registrar._refresh_cred_from_existing = old_refresh
        registrar._try_join_k12_workspaces = old_join
        registrar._run_queues.pop(run_id, None)


if __name__ == "__main__":
    test_registered_k12_join_uses_current_config_and_mail_source()
    test_registered_k12_join_runs_accounts_concurrently()
    test_registered_k12_join_counts_cooldown_as_skipped()
    print("ok")
