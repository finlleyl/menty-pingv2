from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str
    mentor_user_id: int
    llm_api_key: str
    llm_base_url: str = "https://openrouter.ai/api/v1"
    spreadsheet_id: str
    google_sa_path: str = "service_account.json"
    active_sheets: str
    edu_base_url: str = "https://edu.gomafia.co"
    edu_email: str = ""
    edu_password: str = ""
    llm_model_smart: str = "openai/gpt-5.6-sol"
    llm_model_fast: str = "openai/gpt-5.6-luna"
    embed_model: str = "openai/text-embedding-3-small"
    db_path: str = "data/bot.db"
    kb_path: str = "data/kb"
    tz_name: str = "Europe/Moscow"
    ping_interval_days: int = 3
    quiet_start_hour: int = 11
    quiet_end_hour: int = 20
    max_unanswered_pings: int = 3
    debounce_minutes: int = 5
    dossier_hour: int = 3
    backup_hour: int = 4          # -1 — ночной бэкап выключен
    digest_weekday: str = "mon"   # день недельной сводки (mon..sun)
    digest_hour: int = 10
    stop_statuses: str = "умер,оффер,приостановил,договор,ушел,ушёл,на стопе"
    log_level: str = "INFO"
    # точные заголовки колонок (регистр не важен), если бот путает их сам; пусто — угадывает
    header_mentee: str = ""
    header_date: str = ""
    header_status: str = ""
    header_notes: str = ""
    header_dossier: str = ""

    @property
    def active_sheet_titles(self) -> list[str]:
        return [t.strip() for t in self.active_sheets.split(",") if t.strip()]

    @property
    def stop_status_list(self) -> list[str]:
        return [t.strip().lower() for t in self.stop_statuses.split(",") if t.strip()]

    @property
    def header_overrides(self) -> dict[str, str]:
        """Роль колонки → заголовок из HEADER_*, только заданные — для SheetsClient."""
        pairs = {"mentee": self.header_mentee, "date": self.header_date,
                 "status": self.header_status, "notes": self.header_notes,
                 "dossier": self.header_dossier}
        return {role: v.strip() for role, v in pairs.items() if v.strip()}


def load_settings() -> Settings:
    return Settings()
