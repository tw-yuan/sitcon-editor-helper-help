import pytest


@pytest.fixture
def required_env(monkeypatch):
    values = {
        "TELEGRAM_BOT_TOKEN": "test-token",
        "TELEGRAM_ADMIN_ID": "7",
        "GITLAB_TOKEN": "test-gitlab",
        "LLM_API_KEY": "test-llm",
        "LLM_MODEL": "test-model",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values
