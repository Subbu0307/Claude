import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    model: str = field(default_factory=lambda: _env("MARKETPLACE_MODEL", "claude-opus-5-5"))
    effort: str = field(default_factory=lambda: _env("MARKETPLACE_EFFORT", "low"))
    db_path: str = field(default_factory=lambda: _env("MARKETPLACE_DB_PATH", "marketplace.db"))
    currency: str = field(default_factory=lambda: _env("MARKETPLACE_CURRENCY", "USD"))

    whatsapp_access_token: str = field(default_factory=lambda: _env("WHATSAPP_ACCESS_TOKEN"))
    whatsapp_phone_number_id: str = field(default_factory=lambda: _env("WHATSAPP_PHONE_NUMBER_ID"))
    whatsapp_verify_token: str = field(default_factory=lambda: _env("WHATSAPP_VERIFY_TOKEN"))
    whatsapp_app_secret: str = field(default_factory=lambda: _env("WHATSAPP_APP_SECRET"))
    whatsapp_api_version: str = field(default_factory=lambda: _env("WHATSAPP_API_VERSION", "v21.0"))
