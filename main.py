from __future__ import annotations

import getpass
from html.parser import HTMLParser
import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from config import Config, load_config


class BmcApiError(RuntimeError):
    pass


class LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_login_form = False
        self.action = "/rsso/start"
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}

        if tag == "form" and attrs_dict.get("id") == "login_form":
            self.in_login_form = True
            self.action = attrs_dict.get("action") or self.action
            return

        if tag == "input" and self.in_login_form:
            name = attrs_dict.get("name")
            if name:
                self.fields[name] = attrs_dict.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self.in_login_form:
            self.in_login_form = False


def _request(
    method: str,
    url: str,
    *,
    opener: urllib.request.OpenerDirector | None = None,
    token: str | None = None,
    data: bytes | None = None,
    content_type: str | None = None,
    headers: dict[str, str] | None = None,
) -> bytes:
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)

    if token:
        request_headers["Authorization"] = f"AR-JWT {token}"

    if content_type:
        request_headers["Content-Type"] = content_type

    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)

    try:
        open_url = opener.open if opener else urllib.request.urlopen
        with open_url(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace").strip()
        message = f"{method} {url} failed with HTTP {error.code} {error.reason}"
        if error_body:
            message = f"{message}: {error_body}"
        raise BmcApiError(message) from error
    except urllib.error.URLError as error:
        raise BmcApiError(f"{method} {url} failed: {error.reason}") from error


def _login_error_hint(config: Config) -> str:
    return (
        "Login failed. Verify that this BMC environment exposes the REST login "
        f"endpoint at {config.base_url}/api/jwt/login. If the browser login uses "
        "a different SSO flow, ask the BMC admin whether REST access uses AR-JWT, "
        "OAuth2, or a different API base path."
    )


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


def rsso_login(config: Config, username: str, password: str) -> urllib.request.OpenerDirector:
    if not config.rsso_url:
        raise BmcApiError("BMC_RSSO_URL is required for RSSO browser-like login")

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    start_url = f"{config.rsso_url}/rsso/start"
    goto_url = f"{config.base_url}/arsys/"

    hash_handler_payload = {
        "url_hash_handler": "true",
        "goto": goto_url,
    }
    if config.rsso_tenant:
        hash_handler_payload["tenant"] = config.rsso_tenant

    login_page = _request(
        "POST",
        start_url,
        opener=opener,
        data=urllib.parse.urlencode(hash_handler_payload).encode("utf-8"),
        content_type="application/x-www-form-urlencoded",
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": "Mozilla/5.0",
        },
    ).decode("utf-8", errors="replace")

    parser = LoginFormParser()
    parser.feed(login_page)

    if not parser.fields:
        preview = " ".join(login_page.split())[:500]
        raise BmcApiError(f"RSSO login form was not found. Response preview: {preview}")

    login_payload = dict(parser.fields)
    login_payload["user-name"] = username
    login_payload["password"] = password

    login_action = urllib.parse.urljoin(config.rsso_url, parser.action)
    response_body = _request(
        "POST",
        login_action,
        opener=opener,
        data=urllib.parse.urlencode(login_payload).encode("utf-8"),
        content_type="application/x-www-form-urlencoded",
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": "Mozilla/5.0",
        },
    ).decode("utf-8", errors="replace")

    if "login_form" in response_body or "login-error-message" in response_body:
        raise BmcApiError("RSSO login did not complete; check username/password")

    return opener


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


def fetch_incidents_with_rsso(
    config: Config,
    opener: urllib.request.OpenerDirector,
) -> dict[str, Any]:
    url = f"{config.base_url}/arsys/api/com.bmc.dsm.itsm.itsm-rest-api/incident/search"
    search_payload: dict[str, Any] = {
        "startIndex": 0,
        "pageSize": config.page_size,
    }

    if config.assignee_group:
        search_payload["assigneeGroup"] = config.assignee_group

    response_body = _request(
        "POST",
        url,
        opener=opener,
        data=json.dumps(search_payload).encode("utf-8"),
        content_type="application/json",
    )

    return json.loads(response_body.decode("utf-8"))


def main() -> None:
    config = load_config()
    username = input("BMC username: ").strip()
    password = getpass.getpass("BMC password: ")

    try:
        if config.rsso_url:
            opener = rsso_login(config, username, password)
            incidents = fetch_incidents_with_rsso(config, opener)
        else:
            token = login(config, username, password)
            incidents = fetch_incidents(config, token)
    except BmcApiError as error:
        print(f"Error: {error}", file=sys.stderr)
        if "/api/jwt/login" in str(error):
            print(_login_error_hint(config), file=sys.stderr)
        sys.exit(1)

    print(json.dumps(incidents, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
