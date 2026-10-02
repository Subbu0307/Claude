import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Spot:
    code: str
    name: str
    route: str = ""


@dataclass
class FinderConfig:
    site_name: str
    whatsapp_number: str
    spots: dict[str, Spot]
    retention_hours: float = 12

    # Secrets come from the environment, never from the YAML file.
    access_token: str = field(default_factory=lambda: os.environ.get("WHATSAPP_ACCESS_TOKEN", ""))
    phone_number_id: str = field(default_factory=lambda: os.environ.get("WHATSAPP_PHONE_NUMBER_ID", ""))
    verify_token: str = field(default_factory=lambda: os.environ.get("WHATSAPP_VERIFY_TOKEN", ""))
    app_secret: str = field(default_factory=lambda: os.environ.get("WHATSAPP_APP_SECRET", ""))
    api_version: str = field(default_factory=lambda: os.environ.get("WHATSAPP_API_VERSION", "v21.0"))
    db_path: str = field(default_factory=lambda: os.environ.get("FINDER_DB_PATH", "finder.db"))
    stats_password: str = field(default_factory=lambda: os.environ.get("FINDER_STATS_PASSWORD", ""))


def parse_config(raw: dict) -> FinderConfig:
    spots = {}
    for s in raw["spots"]:
        code = str(s["code"]).strip().upper()
        if not code.isalnum():
            raise ValueError(f"Spot code {code!r} must be letters and digits only, e.g. E1")
        if code in spots:
            raise ValueError(f"Duplicate spot code {code}")
        spots[code] = Spot(code, s["name"], s.get("route", ""))
    if not spots:
        raise ValueError("Configure at least one spot")
    number = "".join(ch for ch in str(raw.get("whatsapp_number", "")) if ch.isdigit())
    return FinderConfig(
        site_name=raw.get("site_name", "Temple"),
        whatsapp_number=number,
        spots=spots,
        retention_hours=float(raw.get("retention_hours", 12)),
    )


def load_config(path: str | Path) -> FinderConfig:
    return parse_config(yaml.safe_load(Path(path).read_text()))
