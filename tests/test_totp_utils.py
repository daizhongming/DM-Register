import pytest

from totp_utils import normalize_totp_secret, totp_code, verify_totp_code


def test_totp_rfc6238_sha1_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert totp_code(secret, for_time=59, digits=8) == "94287082"
    assert totp_code(secret, for_time=1111111109, digits=8) == "07081804"


def test_totp_accepts_otpauth_uri_and_verifies_window():
    uri = "otpauth://totp/OpenAI:user@example.com?secret=JBSWY3DPEHPK3PXP&issuer=OpenAI"
    code = totp_code(uri, for_time=1_700_000_000)
    assert normalize_totp_secret(uri) == "JBSWY3DPEHPK3PXP"
    assert verify_totp_code(uri, code, at_time=1_700_000_029)


def test_totp_rejects_missing_secret():
    with pytest.raises(ValueError):
        normalize_totp_secret("otpauth://totp/OpenAI:user@example.com?issuer=OpenAI")
