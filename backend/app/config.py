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

    # Multi-note (v2) limits and retention.
    max_note_bytes: int = 1_000_000
    max_notes_per_user: int = 5000
    trash_retention_days: int = 60  # trashed notes are purged after this (same as OneNote's recycle bin)
    tombstone_retention_days: int = 90  # purged rows stay this long so other devices learn about them
    revision_keep: int = 50  # newest N history snapshots per note
    revision_keep_min: int = 5  # always kept even when older than revision_retention_days
    revision_retention_days: int = 30
    revision_coalesce_seconds: int = 60  # at most one snapshot per note per window (unless a big shrink)
    image_gc_grace_days: int = 30  # an unreferenced image must be at least this old before it is deleted

    # extra="ignore": the same .env also feeds docker-compose (POSTGRES_* etc.).
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
