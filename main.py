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


class FormParser(HTMLParser):
    def __init__(self, form_id: str | None) -> None:
        super().__init__()
        self.form_id = form_id
        self.in_form = False
        self.found = False
        self.action = ""
        self.method = "get"
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}

        if tag == "form" and not self.found:
            if self.form_id is not None and attrs_dict.get("id") != self.form_id:
                return

            self.in_form = True
            self.found = True
            self.action = attrs_dict.get("action", "")
            self.method = attrs_dict.get("method", "get").lower()
            return

        if tag == "input" and self.in_form:
            name = attrs_dict.get("name")
            if name:
                self.fields[name] = attrs_dict.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self.in_form:
            self.in_form = False


class LoginErrorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_error_message = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        classes = attrs_dict.get("class", "").split()

        if (
            attrs_dict.get("id") == "login-error-message"
            or "login-error-message" in classes
        ):
            self.in_error_message += 1
        elif self.in_error_message:
            self.in_error_message += 1

    def handle_endtag(self, tag: str) -> None:
        if self.in_error_message:
            self.in_error_message -= 1

    def handle_data(self, data: str) -> None:
        if self.in_error_message:
            text = data.strip()
            if text:
                self.parts.append(text)


def _extract_login_error(html: str) -> str | None:
    parser = LoginErrorParser()
    parser.feed(html)
    message = " ".join(parser.parts)
    return message or None


class HtmlSummaryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.title_parts: list[str] = []
        self.forms: list[dict[str, str]] = []
        self.selects: list[dict[str, Any]] = []
        self.current_select: dict[str, Any] | None = None
        self.current_option: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}

        if tag == "title":
            self.in_title = True
        elif tag == "form":
            self.forms.append(
                {
                    "id": attrs_dict.get("id", ""),
                    "name": attrs_dict.get("name", ""),
                    "action": attrs_dict.get("action", ""),
                    "method": attrs_dict.get("method", ""),
                }
            )
        elif tag == "select":
            self.current_select = {
                "id": attrs_dict.get("id", ""),
                "name": attrs_dict.get("name", ""),
                "options": [],
            }
            self.selects.append(self.current_select)
        elif tag == "option" and self.current_select is not None:
            self.current_option = {
                "value": attrs_dict.get("value", ""),
                "text": "",
            }

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        elif tag == "select":
            self.current_select = None
        elif tag == "option" and self.current_select is not None and self.current_option:
            options = self.current_select["options"]
            options.append(
                {
                    "value": self.current_option["value"],
                    "text": " ".join(self.current_option["text"].split()),
                }
            )
            self.current_option = None

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data.strip())
        elif self.current_option is not None:
            self.current_option["text"] += data


def _summarize_html_page(html: str) -> str:
    parser = HtmlSummaryParser()
    parser.feed(html)

    parts: list[str] = []
    title = " ".join(" ".join(parser.title_parts).split())
    if title:
        parts.append(f"title={title!r}")

    if parser.forms:
        form_parts = []
        for form in parser.forms[:5]:
            form_parts.append(
                "form("
                f"id={form['id']!r}, "
                f"name={form['name']!r}, "
                f"action={form['action']!r}, "
                f"method={form['method']!r}"
                ")"
            )
        parts.append("forms=" + "; ".join(form_parts))

    if parser.selects:
        select_parts = []
        for select in parser.selects[:5]:
            options = select["options"]
            option_parts = [
                f"{option['text'] or option['value']!r}"
                for option in options[:10]
            ]
            select_parts.append(
                "select("
                f"id={select['id']!r}, "
                f"name={select['name']!r}, "
                f"options=[{', '.join(option_parts)}]"
                ")"
            )
        parts.append("selects=" + "; ".join(select_parts))

    return " | ".join(parts) or "no html title/forms/selects found"


def _preview_response_body(body: str) -> str:
    return " ".join(body.split())[:500]


def _set_first_matching_field(
    payload: dict[str, str],
    candidates: tuple[str, ...],
    value: str,
) -> bool:
    for candidate in candidates:
        if candidate in payload:
            payload[candidate] = value
            return True
    return False


def _browser_start_url(config: Config) -> str:
    if config.browser_start_url:
        return config.browser_start_url

    query = {}
    if config.auth_string:
        query["user_domain"] = config.auth_string

    encoded_query = urllib.parse.urlencode(query)
    if encoded_query:
        return f"{config.base_url}/arsys/?{encoded_query}"
    return f"{config.base_url}/arsys/"


def _submit_form(
    parser: FormParser,
    page_url: str,
    opener: urllib.request.OpenerDirector,
) -> tuple[str, str]:
    action = urllib.parse.urljoin(page_url, parser.action or page_url)
    payload = urllib.parse.urlencode(parser.fields).encode("utf-8")
    method = parser.method.upper()

    if method == "POST":
        response_body, response_url = _request_with_url(
            "POST",
            action,
            opener=opener,
            data=payload,
            content_type="application/x-www-form-urlencoded",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "User-Agent": "Mozilla/5.0",
            },
        )
        return response_body.decode("utf-8", errors="replace"), response_url

    if payload:
        separator = "&" if urllib.parse.urlparse(action).query else "?"
        action = f"{action}{separator}{payload.decode('utf-8')}"

    response_body, response_url = _request_with_url(
        "GET",
        action,
        opener=opener,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": "Mozilla/5.0",
        },
    )
    return response_body.decode("utf-8", errors="replace"), response_url


def _submit_auto_form_if_present(
    html: str,
    page_url: str,
    opener: urllib.request.OpenerDirector,
    *,
    form_id: str | None = None,
) -> tuple[str, str]:
    parser = FormParser(form_id)
    parser.feed(html)

    if not parser.found:
        return html, page_url

    return _submit_form(parser, page_url, opener)


def _follow_auto_submit_pages(
    html: str,
    page_url: str,
    opener: urllib.request.OpenerDirector,
) -> tuple[str, str]:
    for _ in range(5):
        previous_html = html
        previous_url = page_url

        html, page_url = _submit_auto_form_if_present(
            html,
            page_url,
            opener,
            form_id="hashHandlerForm",
        )
        if (html, page_url) != (previous_html, previous_url):
            continue

        if "document.forms[0].submit()" not in html:
            break

        html, page_url = _submit_auto_form_if_present(html, page_url, opener)
        if (html, page_url) == (previous_html, previous_url):
            break

    return html, page_url


def _request_with_url(
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
            return response.read(), response.geturl()
    except urllib.error.HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace").strip()
        message = f"{method} {url} failed with HTTP {error.code} {error.reason}"
        if error_body:
            message = f"{message}: {error_body}"
        raise BmcApiError(message) from error
    except urllib.error.URLError as error:
        raise BmcApiError(f"{method} {url} failed: {error.reason}") from error


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
    response_body, _ = _request_with_url(
        method,
        url,
        opener=opener,
        token=token,
        data=data,
        content_type=content_type,
        headers=headers,
    )
    return response_body


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
    start_url = _browser_start_url(config)

    login_page_body, login_page_url = _request_with_url(
        "GET",
        start_url,
        opener=opener,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": "Mozilla/5.0",
        },
    )
    login_page = login_page_body.decode("utf-8", errors="replace")
    login_page, login_page_url = _follow_auto_submit_pages(
        login_page,
        login_page_url,
        opener,
    )

    parser = LoginFormParser()
    parser.feed(login_page)

    if not parser.fields:
        preview = " ".join(login_page.split())[:500]
        raise BmcApiError(f"RSSO login form was not found. Response preview: {preview}")

    login_payload = dict(parser.fields)

    username_was_set = _set_first_matching_field(
        login_payload,
        ("user-name", "username", "user", "j_username"),
        username,
    )
    password_was_set = _set_first_matching_field(
        login_payload,
        ("password", "passwd", "j_password"),
        password,
    )
    if config.auth_string:
        login_payload["authString"] = config.auth_string
    if config.rsso_tenant:
        login_payload["tenant"] = config.rsso_tenant

    if not username_was_set:
        login_payload["user-name"] = username
    if not password_was_set:
        login_payload["password"] = password

    login_action = urllib.parse.urljoin(login_page_url, parser.action)
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
        login_error = _extract_login_error(response_body)
        raw_error = _preview_response_body(response_body)
        html_summary = _summarize_html_page(response_body)
        if login_error:
            raise BmcApiError(
                f"RSSO login failed: {login_error}: {html_summary}: {raw_error}"
            )
        raise BmcApiError(
            "RSSO login failed: no explicit error message returned: "
            f"{html_summary}: {raw_error}"
        )

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
