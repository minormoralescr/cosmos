"""Configuracion central (single source of truth, cacheada via lru_cache -> DRY).

Lectura desde entorno + .env con pydantic-settings v2.
Todos los modulos importan `settings` desde aqui; nunca leen env vars directamente.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuracion tipada de la app. Sobreescribible via entorno / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---
    app_name: str = "Spa WhatsApp"
    app_base_url: str = "https://spa.example.com"  # para links absolutos en mensajes
    env: str = "dev"  # dev | prod
    business_tz: str = "Europe/Madrid"  # tz del spa: ventanas de Availability y slots

    # --- PostgreSQL (async). Driver asyncpg: NUNCA sqlite. ---
    database_url: str = "postgresql+asyncpg://spa:spa_secret@localhost:5432/spa"
    db_echo: bool = False

    # --- Twilio WhatsApp Business API ---
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = "whatsapp:+14155238886"
    # Twilio valida la firma del webhook con AccountSid + AuthToken (HMAC-SHA1).

    # --- Auth JWT ---
    jwt_secret: str = "change-me-in-prod"
    jwt_alg: str = "HS256"
    jwt_expire_min: int = 720  # 12h

    # --- z.ai (agente IA: clasificador de intencion + generacion de respuesta) ---
    zai_api_key: str = ""
    zai_base_url: str = "https://api.z.ai/api/paas/v4"
    zai_model: str = "glm-4-flash"  # rapido + economico para routing de intencion
    ai_agent_enabled: bool = True

    # --- SMTP (email de respaldo de recordatorios) ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""

    # --- Scheduler (APScheduler SQLAlchemyJobStore sobre el mismo Postgres) ---
    scheduler_jobstore_table: str = "apscheduler_jobs"

    # --- Feature flags ---
    # MVP: Google Calendar sync OFF. Se anade despues (Fase 6+).
    google_sync_enabled: bool = False

    @computed_field
    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache
def get_settings() -> Settings:
    """Singleton cacheado. Tests pueden limpiar cache con get_settings.cache_clear()."""
    return Settings()


settings = get_settings()
