from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./notes_sync.db"
    jwt_secret_key: str = "change-me-to-a-long-random-string"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30
    # Must match the desktop client's Google OAuth "Desktop app" client ID
    # (app/sync/google_oauth.py) — it's the audience Google ID tokens are checked against.
    google_client_id: str = "379908990291-36934dgf21e8u90qj8f5ldnoij76ij1k.apps.googleusercontent.com"
    # OAuth client secret for the "Desktop app" client. Lives ONLY here on the
    # server; the desktop client never ships it. Required for the loopback
    # code->token exchange in POST /auth/google.
    google_client_secret: str = ""
    google_token_uri: str = "https://oauth2.googleapis.com/token"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
