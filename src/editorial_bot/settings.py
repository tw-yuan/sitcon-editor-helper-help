"""Explicit configuration for the long-lived editorial team."""

from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    telegram_bot_token: SecretStr
    telegram_admin_id: int
    bot_trigger_name: str = "小石"
    gitlab_url: str = "https://gitlab.com"
    gitlab_project: str = "sitcon-tw/editorial/board"
    gitlab_token: SecretStr
    google_sa_json_path: str = "secrets/google-sa.json"
    roster_sheet_id: str = "1GD4VOSN8UIL4TAo1tBVVt-SPdMXQs6y28XUcxN6_xrE"
    roster_sheet_gid: int = 0
    drive_root_folder_id: str = "1rMkuh-qNk_GcADbZItTh8cM9MhwBJsi6"
    doc_template_id: str = "1ISur15UxlnXVi0vB4zBVqhby2YNGGIViccZI0IiL-LU"
    llm_provider: Literal["openai_compat", "anthropic"] = "openai_compat"
    llm_api_key: SecretStr
    llm_model: str
    llm_base_url: str = ""
    llm_auth_bearer: bool = False
    llm_service_tier: str = ""
    llm_thinking: Literal["off", "low", "medium", "high", "xhigh", "max"] = "off"
    llm_web_search: bool = False
    llm_max_tool_iterations: int = Field(12, ge=1, le=32)
    web_search_api_key: SecretStr = SecretStr("")
    web_search_model: str = ""
    web_search_base_url: str = ""
    web_search_auth_bearer: bool = False
    web_search_max_uses: int = Field(0, ge=0)
    web_search_max_content_tokens: int = Field(40000, ge=1000)
    web_search_country: str = "TW"
    web_search_timeout: float = Field(180, gt=0)
    db_path: str = "data/editorial.sqlite3"
    migrations_path: str = "migrations"
    tz: str = "Asia/Taipei"
    cache_ttl_roster: int = Field(300, ge=0)
    cache_ttl_wiki: int = Field(900, ge=0)
    context_ttl_seconds: int = Field(1800, ge=1)
    max_concurrent_agent_turns: int = Field(4, ge=1, le=32)
    default_document_label: str = "社群文案"
    default_task_label: str = "編輯組專案"
    initial_status: str = "Status::Inbox"
    review_status: str = "Status::Review"
    log_level: str = "INFO"

    @field_validator("gitlab_url")
    @classmethod
    def validate_gitlab_url(cls, value: str) -> str:
        if value.rstrip("/") != "https://gitlab.com":
            raise ValueError("此部署只操作 https://gitlab.com")
        return value.rstrip("/")

    @field_validator("gitlab_project")
    @classmethod
    def validate_project(cls, value: str) -> str:
        if value != "sitcon-tw/editorial/board":
            raise ValueError("此部署只操作 sitcon-tw/editorial/board")
        return value
