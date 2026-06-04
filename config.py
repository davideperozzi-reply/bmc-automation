from __future__ import annotations

from dataclasses import dataclass
import os

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    base_url: str
    rsso_url: str | None = None
    browser_start_url: str | None = None
    rsso_tenant: str | None = None
    auth_string: str | None = None
    assignee_group: str | None = None
    page_size: int = 10


def _required(values: dict[str, str], key: str) -> str:
    value = values.get(key)
    if not value:
        raise ValueError(f"Missing required config value: {key}")
    return value


def load_config() -> Config:
    values = os.environ

    return Config(
        base_url=_required(values, "BMC_BASE_URL").rstrip("/"),
        rsso_url=(values.get("BMC_RSSO_URL") or "").rstrip("/") or None,
        browser_start_url=values.get("BMC_BROWSER_START_URL") or None,
        rsso_tenant=values.get("BMC_RSSO_TENANT") or None,
        auth_string=values.get("BMC_AUTH_STRING") or None,
        assignee_group=values.get("BMC_ASSIGNEE_GROUP") or None,
        page_size=int(values.get("BMC_PAGE_SIZE", "10")),
    )
