from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str
    mentor_user_id: int
    openai_api_key: str
    spreadsheet_id: str
    google_sa_path: str = "service_account.json"
    active_sheets: str
    edu_base_url: str = "https://edu.gomafia.co"
    edu_email: str = ""
    edu_password: str = ""
    llm_model_smart: str = "gpt-5.1"
    llm_model_fast: str = "gpt-5-mini"
    embed_model: str = "text-embedding-3-small"
    db_path: str = "data/bot.db"
    kb_path: str = "data/kb"
    tz_name: str = "Europe/Moscow"
    ping_interval_days: int = 3
    quiet_start_hour: int = 11
    quiet_end_hour: int = 20
    max_unanswered_pings: int = 3
    debounce_minutes: int = 5
    dossier_hour: int = 3
    stop_statuses: str = "умер,оффер,приостановил,договор,ушел,ушёл,на стопе"
    log_level: str = "INFO"

    @property
    def active_sheet_titles(self) -> list[str]:
        return [t.strip() for t in self.active_sheets.split(",") if t.strip()]

    @property
    def stop_status_list(self) -> list[str]:
        return [t.strip().lower() for t in self.stop_statuses.split(",") if t.strip()]


def load_settings() -> Settings:
    return Settings()
