from sms_provider import NexSmsProvider, PhoneCallbackController


def test_nexsms_preferred_activation_is_used_once_without_renting():
    ctrl = PhoneCallbackController(
        "nexsms",
        {"sms_api_key": "dummy", "sms_country": "US", "sms_service": "openai"},
    )
    ctrl.provider = NexSmsProvider(api_key="dummy")
    ctrl.set_preferred_activation("order-1", "+12194185972")

    assert ctrl.get_phone() == "+12194185972"
    assert ctrl.activation.activation_id == "order-1"
    assert ctrl.activation.metadata["preferred"] is True
    assert ctrl._preferred_activation_used is True
