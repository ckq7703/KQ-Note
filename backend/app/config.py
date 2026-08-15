from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./notes_sync.db"
    jwt_secret_key: str = "change-me-to-a-long-random-string"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30
    # Must match the desktop client's Google OAuth "Desktop app" client ID
    # (app/sync/google_client_secret.json) — it's the audience Google ID tokens are checked against.
    google_client_id: str = "138730307134-n3jqje303i9roscbcmdho9ep9c702q0j.apps.googleusercontent.com"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
