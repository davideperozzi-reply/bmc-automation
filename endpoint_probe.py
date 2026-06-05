from __future__ import annotations

import argparse
import getpass
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from config import Config, load_config


DEFAULT_ENDPOINTS = (
    "/api/com.bmc.dsm.itsm.itsm-rest-api/incident/search",
    "/arsys/api/com.bmc.dsm.itsm.itsm-rest-api/incident/search",
)


@dataclass(frozen=True)
class ProbeResult:
    method: str
    url: str
    ok: bool
    status: int | None
    reason: str
    content_type: str
    body_preview: str


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Probe BMC endpoints using a bearer token or cookies copied manually "
            "from the browser after operator login."
        )
    )
    parser.add_argument(
        "--endpoint",
        action="append",
        help=(
            "Endpoint to test. May be absolute or relative to BMC_BASE_URL. "
            "Can be repeated. Defaults to incident search endpoints."
        ),
    )
    parser.add_argument(
        "--method",
        default="POST",
        help="HTTP method to use for every probe. Default: POST.",
    )
    parser.add_argument(
        "--auth-scheme",
        default="Bearer",
        help="Authorization header scheme for token mode. Default: Bearer.",
    )
    parser.add_argument(
        "--cookie",
        action="store_true",
        help="Use a browser Cookie header instead of an Authorization bearer token.",
    )
    parser.add_argument(
        "--body",
        help=(
            "JSON request body. If omitted, a minimal incident search payload is used "
            "for methods that usually accept a body."
        ),
    )
    parser.add_argument(
        "--preview-chars",
        type=int,
        default=700,
        help="Maximum response body characters to print. Default: 700.",
    )
    return parser.parse_args()


def _authorization_header(token: str, auth_scheme: str) -> str:
    auth_scheme = auth_scheme.strip()
    if not auth_scheme:
        return token
    return f"{auth_scheme} {token}"


def _resolve_url(config: Config, endpoint: str) -> str:
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme and parsed.netloc:
        return endpoint

    path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    return f"{config.base_url}{path}"


def _default_payload(config: Config) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "startIndex": 0,
        "pageSize": 1,
    }
    if config.assignee_group:
        payload["assigneeGroup"] = config.assignee_group
    return payload


def _request_body(args: argparse.Namespace, config: Config) -> bytes | None:
    method = args.method.upper()
    if method in {"GET", "HEAD", "DELETE"}:
        return None

    if args.body:
        try:
            return json.dumps(json.loads(args.body)).encode("utf-8")
        except json.JSONDecodeError as error:
            raise ValueError(f"--body is not valid JSON: {error}") from error

    return json.dumps(_default_payload(config)).encode("utf-8")


def _preview(body: bytes, limit: int) -> str:
    text = body.decode("utf-8", errors="replace")
    return " ".join(text.split())[:limit]


def _reason_for_status(status: int | None, body_preview: str) -> str:
    if status is None:
        return "network/error before HTTP response"
    if 200 <= status < 300:
        return "reachable"
    if status in {301, 302, 303, 307, 308}:
        return "redirected, likely browser/SSO flow or wrong base path"
    if status == 400:
        return "endpoint reached, but request payload/query may be wrong"
    if status == 401:
        return "unauthorized, credentials were rejected or expired"
    if status == 403:
        return "forbidden, token accepted but blocked by permissions/policy"
    if status == 404:
        return "not found, endpoint path is probably wrong for this environment"
    if status == 405:
        return "method not allowed, try a different --method"
    if status == 415:
        return "unsupported media type, content type or payload format may be wrong"
    if "login" in body_preview.lower() or "sso" in body_preview.lower():
        return "response looks like an auth/login page"
    return "HTTP error returned by server"


def probe_endpoint(
    *,
    method: str,
    url: str,
    authorization: str | None,
    cookie: str | None,
    auth_scheme: str,
    body: bytes | None,
    preview_chars: int,
) -> ProbeResult:
    headers = {
        "Accept": "application/json",
        "User-Agent": "bmc-endpoint-probe/0.1",
    }
    if authorization:
        headers["Authorization"] = _authorization_header(authorization, auth_scheme)
    if cookie:
        headers["Cookie"] = cookie
    if body is not None:
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=method.upper(),
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = response.read()
            body_preview = _preview(response_body, preview_chars)
            status = response.status
            return ProbeResult(
                method=method.upper(),
                url=response.geturl(),
                ok=200 <= status < 300,
                status=status,
                reason=_reason_for_status(status, body_preview),
                content_type=response.headers.get("Content-Type", ""),
                body_preview=body_preview,
            )
    except urllib.error.HTTPError as error:
        response_body = error.read()
        body_preview = _preview(response_body, preview_chars)
        return ProbeResult(
            method=method.upper(),
            url=url,
            ok=False,
            status=error.code,
            reason=_reason_for_status(error.code, body_preview),
            content_type=error.headers.get("Content-Type", ""),
            body_preview=body_preview,
        )
    except urllib.error.URLError as error:
        return ProbeResult(
            method=method.upper(),
            url=url,
            ok=False,
            status=None,
            reason=f"{_reason_for_status(None, '')}: {error.reason}",
            content_type="",
            body_preview="",
        )


def _print_result(result: ProbeResult) -> None:
    status = result.status if result.status is not None else "NO_HTTP_RESPONSE"
    outcome = "OK" if result.ok else "FAIL"

    print(f"[{outcome}] {result.method} {result.url}")
    print(f"  status: {status}")
    print(f"  reason: {result.reason}")
    if result.content_type:
        print(f"  content-type: {result.content_type}")
    if result.body_preview:
        print(f"  body-preview: {result.body_preview}")
    print()


def main() -> None:
    args = _parse_args()
    config = load_config()
    endpoints = args.endpoint or list(DEFAULT_ENDPOINTS)

    try:
        body = _request_body(args, config)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(2)

    authorization: str | None = None
    cookie: str | None = None
    if args.cookie:
        cookie = getpass.getpass("Cookie header copied from browser: ").strip()
        if not cookie:
            print("Error: cookie header is required", file=sys.stderr)
            sys.exit(2)
    else:
        authorization = getpass.getpass("Bearer token copied from browser: ").strip()

    if not authorization and not cookie:
        print("Error: bearer token or cookie header is required", file=sys.stderr)
        sys.exit(2)

    results = [
        probe_endpoint(
            method=args.method,
            url=_resolve_url(config, endpoint),
            authorization=authorization,
            cookie=cookie,
            auth_scheme=args.auth_scheme,
            body=body,
            preview_chars=args.preview_chars,
        )
        for endpoint in endpoints
    ]

    for result in results:
        _print_result(result)

    if not any(result.ok for result in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
