from openai_mfa import playwright_storage_state


def test_playwright_storage_state_uses_session_token():
    state = playwright_storage_state({
        "session_token": "st",
        "cookie_header": "oai-did=did",
    })
    cookies = {c["name"]: c for c in state["cookies"]}
    assert cookies["__Secure-next-auth.session-token"]["value"] == "st"
    assert cookies["__Secure-next-auth.session-token"]["httpOnly"] is True
    assert cookies["oai-did"]["value"] == "did"
