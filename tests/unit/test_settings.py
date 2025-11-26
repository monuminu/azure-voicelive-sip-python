from voicelive_sip_gateway.config.settings import load_settings


def test_load_settings_reads_env(monkeypatch):
    load_settings.cache_clear()
    monkeypatch.setenv("AZURE_VOICELIVE_ENDPOINT", "wss://example")
    settings = load_settings()

    assert settings.azure.endpoint == "wss://example"
    assert settings.sip.port == 5060
