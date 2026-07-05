import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth_flow import AuthFlow
from config import Config


def test_signup_continues_passwordless_existing_account():
    flow = AuthFlow(Config())
    flow.authorize_continue = lambda **kwargs: {
        "page": {
            "type": "email_otp_verification",
            "payload": {"email_verification_mode": "passwordless_signup"},
        }
    }

    is_new = flow.signup("used@example.com", "sentinel")
    flow.result.login_mode = not is_new

    assert is_new is False
    assert flow._is_existing_account is True
    assert flow.result.to_dict()["login_mode"] is True


if __name__ == "__main__":
    test_signup_continues_passwordless_existing_account()
