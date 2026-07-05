import json
import sys
import tempfile
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mail_paymesh import PayMeshCardEmailProvider
from webui import db


class FakeResp:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, redeems, lookups):
        self.redeems = list(redeems)
        self.lookups = list(lookups)

    def post(self, *args, **kwargs):
        return FakeResp(self.redeems.pop(0))

    def get(self, *args, **kwargs):
        if len(self.lookups) > 1:
            return FakeResp(self.lookups.pop(0))
        return FakeResp(self.lookups[0])


def email_lookup(address="demo@example.com", status="active", codes=None):
    return {
        "code": 0,
        "data": {
            "type": "email",
            "email": {
                "session": {"emailAddress": address, "status": status},
                "codes": codes or [],
            },
        },
    }


def test_paymesh_card_flow_and_state():
    old_db_path = db.DB_PATH
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db.DB_PATH = Path(tmp) / "webui.db"
            db.init_db()
            db.save_mail_config({
                "mail_source": "paymesh_card",
                "paymesh_card_codes": "CARD-0001-AAAA\n# comment\nCARD-0001-AAAA\nCARD-0002-BBBB",
            })

            cfg = db.get_mail_config()
            assert "paymesh_base_url" not in cfg
            assert "paymesh_cards" not in cfg
            assert cfg["paymesh_card_codes"] == "CARD-0001-AAAA\nCARD-0002-BBBB"
            assert cfg["paymesh_card_count"] == 2
            assert cfg["paymesh_card_stats"]["available"] == 2

            first = db.claim_paymesh_card()
            assert first["code"] == "CARD-0001-AAAA"
            assert db.claim_paymesh_card()["code"] == "CARD-0002-BBBB"
            assert db.claim_paymesh_card() is None

            db.save_mail_config({"paymesh_card_codes": "CARD-0002-BBBB\nCARD-0003-CCCC"})
            cfg = db.get_mail_config()
            assert cfg["paymesh_card_codes"] == "CARD-0002-BBBB\nCARD-0003-CCCC"
            assert cfg["paymesh_card_count"] == 2
    finally:
        db.DB_PATH = old_db_path


def test_paymesh_provider_redeem_lookup_and_otp():
    old_db_path = db.DB_PATH
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db.DB_PATH = Path(tmp) / "webui.db"
            db.init_db()
            db.set_setting("paymesh_card_codes", "CARD-OK")
            fake = FakeSession(
                redeems=[{"code": 2004, "msg": "使用中"}],
                lookups=[
                    email_lookup(codes=[]),
                    email_lookup(codes=[
                        {"id": 1, "code": "111111", "receivedAt": "2026-07-04T10:00:00"},
                        {"id": 2, "code": "222222", "receivedAt": "2026-07-04T10:01:00"},
                    ]),
                ],
            )
            provider = PayMeshCardEmailProvider(session=fake)
            email = provider.create_mailbox()
            assert email == "demo@example.com"
            db.release_paymesh_card(provider.card_code)
            assert db.paymesh_card_stats()["available"] == 1
            assert db.paymesh_card_items()[0]["email"] == "demo@example.com"
            assert provider.wait_for_otp(email, timeout=30) == "222222"
            provider.mark_done()
            assert db.paymesh_card_stats()["done"] == 1
            assert db.paymesh_card_items()[0]["email"] == "demo@example.com"
    finally:
        db.DB_PATH = old_db_path


def test_paymesh_existing_card_mode_does_not_claim_new_card():
    old_db_path = db.DB_PATH
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db.DB_PATH = Path(tmp) / "webui.db"
            db.init_db()
            db.set_setting("paymesh_card_codes", "CARD-OLD\nCARD-NEW")
            db.mark_paymesh_card_done("CARD-OLD", "demo@example.com", "active")

            fake = FakeSession(
                redeems=[{"code": 0}],
                lookups=[
                    email_lookup(codes=[
                        {"id": 3, "code": "333333", "receivedAt": "2026-07-04T10:02:00"},
                    ]),
                ],
            )
            provider = PayMeshCardEmailProvider(
                session=fake,
                card_code="CARD-OLD",
                email="demo@example.com",
            )
            assert provider.create_mailbox() == "demo@example.com"
            assert fake.redeems == [{"code": 0}]
            assert db.claim_paymesh_card()["code"] == "CARD-NEW"
            assert provider.wait_for_otp("demo@example.com", timeout=30) == "333333"
    finally:
        db.DB_PATH = old_db_path


def test_auth_resources_backfills_paymesh_state_by_email():
    old_db_path = db.DB_PATH
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db.DB_PATH = Path(tmp) / "webui.db"
            db.init_db()
            db.set_setting("paymesh_card_codes", "CARD-BACKFILL")
            db.mark_paymesh_card_done("CARD-BACKFILL", "demo@example.com", "active")

            resources = db.get_auth_resources("DEMO@example.com")
            assert resources["mail_source"] == "paymesh_card"
            assert resources["paymesh_card_code"] == "CARD-BACKFILL"
            assert resources["paymesh_card"] == "CARD...FILL"
    finally:
        db.DB_PATH = old_db_path


def test_paymesh_platform_ping_accepts_api_error_json():
    fake = FakeSession(
        redeems=[],
        lookups=[{"code": 2001, "msg": "卡密不存在"}],
    )
    assert PayMeshCardEmailProvider(session=fake).ping_platform()["code"] == 2001


def test_paymesh_transport_error_releases_card():
    old_db_path = db.DB_PATH
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db.DB_PATH = Path(tmp) / "webui.db"
            db.init_db()
            db.set_setting("paymesh_card_codes", "CARD-NET")

            class BrokenProvider(PayMeshCardEmailProvider):
                def _redeem(self, code):
                    raise urllib.error.URLError("Could not resolve host")

            try:
                BrokenProvider(session=FakeSession([], [])).create_mailbox()
                assert False, "transport error should raise"
            except urllib.error.URLError:
                pass
            assert db.paymesh_card_stats()["available"] == 1
            assert db.paymesh_card_stats()["failed"] == 0
    finally:
        db.DB_PATH = old_db_path


def test_paymesh_rejects_non_email_card():
    old_db_path = db.DB_PATH
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db.DB_PATH = Path(tmp) / "webui.db"
            db.init_db()
            db.set_setting("paymesh_card_codes", "CARD-SMS")
            fake = FakeSession(
                redeems=[{"code": 0}],
                lookups=[{"code": 0, "data": {"type": "sms", "sms": {}}}],
            )
            provider = PayMeshCardEmailProvider(session=fake)
            try:
                provider.create_mailbox()
                assert False, "non-email card should fail"
            except RuntimeError:
                pass
            assert db.paymesh_card_stats()["failed"] == 1
    finally:
        db.DB_PATH = old_db_path


def test_paymesh_retry_failed_only_resets_failed_cards():
    old_db_path = db.DB_PATH
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db.DB_PATH = Path(tmp) / "webui.db"
            db.init_db()
            db.set_setting(
                "paymesh_card_codes",
                "CARD-0001-AAAA\nCARD-0002-BBBB\nCARD-0003-CCCC",
            )
            db.mark_paymesh_card_failed("CARD-0001-AAAA", "already_registered")
            db.mark_paymesh_card_done("CARD-0002-BBBB", "done@example.com")
            assert db.claim_paymesh_card()["code"] == "CARD-0003-CCCC"

            assert db.reset_failed_paymesh_cards() == 1
            items = db.paymesh_card_items()
            assert [item["status"] for item in items] == ["available", "done", "in_use"]
            assert items[0]["fail_reason"] == ""
            assert db.paymesh_card_stats()["failed"] == 0
    finally:
        db.DB_PATH = old_db_path


def test_paymesh_expired_session_blocks_reauth_capability():
    old_db_path = db.DB_PATH
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db.DB_PATH = Path(tmp) / "webui.db"
            db.init_db()
            db.set_setting("sms_enabled", "1")
            db.set_setting("sms_api_key", "key")
            db.mark_paymesh_card_done("CARD-EXPIRED", "demo@example.com", "expired")

            capability = db.describe_reauth_capability("demo@example.com")
            assert capability["can_attempt_reauth"] is False
            assert "paymesh_session_expired" in capability["blockers"]
    finally:
        db.DB_PATH = old_db_path


if __name__ == "__main__":
    test_paymesh_card_flow_and_state()
    test_paymesh_provider_redeem_lookup_and_otp()
    test_paymesh_existing_card_mode_does_not_claim_new_card()
    test_auth_resources_backfills_paymesh_state_by_email()
    test_paymesh_platform_ping_accepts_api_error_json()
    test_paymesh_transport_error_releases_card()
    test_paymesh_rejects_non_email_card()
    test_paymesh_retry_failed_only_resets_failed_cards()
    test_paymesh_expired_session_blocks_reauth_capability()
    print("ok")
