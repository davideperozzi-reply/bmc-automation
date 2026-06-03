from __future__ import annotations

import getpass
import json
import urllib.parse
import urllib.request
from typing import Any

from config import Config, load_config


def _request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    data: bytes | None = None,
    content_type: str | None = None,
) -> bytes:
    headers = {"Accept": "application/json"}

    if token:
        headers["Authorization"] = f"AR-JWT {token}"

    if content_type:
        headers["Content-Type"] = content_type

    request = urllib.request.Request(url, data=data, headers=headers, method=method)

    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def login(config: Config, username: str, password: str) -> str:
    login_data = {
        "username": username,
        "password": password,
    }

    if config.auth_string:
        login_data["authString"] = config.auth_string

    payload = urllib.parse.urlencode(login_data).encode("utf-8")

    response_body = _request(
        "POST",
        f"{config.base_url}/api/jwt/login",
        data=payload,
        content_type="application/x-www-form-urlencoded",
    )

    return response_body.decode("utf-8").strip()


def fetch_incidents(config: Config, token: str) -> dict[str, Any]:
    url = f"{config.base_url}/api/com.bmc.dsm.itsm.itsm-rest-api/incident/search"
    search_payload: dict[str, Any] = {
        "startIndex": 0,
        "pageSize": config.page_size,
    }

    if config.assignee_group:
        search_payload["assigneeGroup"] = config.assignee_group

    response_body = _request(
        "POST",
        url,
        token=token,
        data=json.dumps(search_payload).encode("utf-8"),
        content_type="application/json",
    )

    return json.loads(response_body.decode("utf-8"))


def main() -> None:
    config = load_config()
    username = input("BMC username: ").strip()
    password = getpass.getpass("BMC password: ")

    token = login(config, username, password)
    incidents = fetch_incidents(config, token)

    print(json.dumps(incidents, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
