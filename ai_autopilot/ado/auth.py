"""Azure DevOps authentication (PAT + OAuth 2.0 authorization-code flow).

Ported from ``AdoAuthService``. PAT auth (Basic) is the primary path for headless
servers. OAuth support loads/refreshes a token from ``~/.ado-autopilot-token.json``
and can perform an interactive browser login for local/desktop use.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import threading
import uuid
import webbrowser
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import httpx

from ai_autopilot.config import Settings
from ai_autopilot.logging_config import get_logger

_AUTH_URL = "https://app.vssps.visualstudio.com/oauth2/authorize"
_TOKEN_URL = "https://app.vssps.visualstudio.com/oauth2/token"
_DEFAULT_SCOPE = "vso.work_write vso.code_write"


@dataclass
class TokenInfo:
    access_token: str
    refresh_token: str | None
    expires_at: datetime

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at.isoformat(),
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, raw: str) -> TokenInfo:
        data = json.loads(raw)
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.fromisoformat(data["expires_at"]),
        )


class AdoAuthService:
    def __init__(self, config: Settings) -> None:
        self._config = config
        self._log = get_logger("ado.auth")
        self._token_file = Path.home() / ".ado-autopilot-token.json"
        self._token: TokenInfo | None = None
        self._lock = asyncio.Lock()

    async def get_auth_header(self) -> dict[str, str]:
        """Return an Authorization header dict for ADO REST calls."""
        if self._config.ado_pat:
            encoded = base64.b64encode(f":{self._config.ado_pat}".encode("ascii")).decode("ascii")
            return {"Authorization": f"Basic {encoded}"}
        token = await self.get_access_token()
        return {"Authorization": f"Bearer {token}"}

    async def get_access_token(self) -> str:
        if self._config.ado_pat:
            return self._config.ado_pat

        async with self._lock:
            if self._token is None:
                self._token = self._load_token()

            if self._token is not None and not self._token.is_expired:
                return self._token.access_token

            if self._token is not None and self._token.refresh_token:
                refreshed = await self._refresh_token(self._token.refresh_token)
                if refreshed is not None:
                    self._token = refreshed
                    self._save_token(refreshed)
                    return refreshed.access_token

            if not self._config.oauth_app_id or not self._config.oauth_app_secret:
                raise RuntimeError(
                    "No PAT and no OAuth app configured. Set either AUTOPILOT_ADO_PAT "
                    "(Personal Access Token) or AUTOPILOT_OAUTH_APP_ID + "
                    "AUTOPILOT_OAUTH_APP_SECRET (OAuth app)."
                )

            self._token = await self._browser_login()
            self._save_token(self._token)
            return self._token.access_token

    # ── OAuth flow ──────────────────────────────────────────────────────────

    async def _browser_login(self) -> TokenInfo:
        code_holder: dict[str, str] = {}
        state = uuid.uuid4().hex

        server = HTTPServer(("localhost", 0), _make_callback_handler(code_holder))
        port = server.server_address[1]
        redirect_uri = f"http://localhost:{port}/callback"

        auth_uri = (
            f"{_AUTH_URL}?client_id={self._config.oauth_app_id}"
            f"&response_type=Assertion&state={state}"
            f"&scope={quote(_DEFAULT_SCOPE)}&redirect_uri={quote(redirect_uri)}"
        )
        self._log.info("opening browser for Azure DevOps login", url=auth_uri)
        try:
            webbrowser.open(auth_uri)
        except Exception:  # best-effort
            self._log.info("could not open browser automatically; visit the url manually")

        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        await asyncio.get_event_loop().run_in_executor(None, thread.join, 300)
        server.server_close()

        if code_holder.get("state") != state:
            raise RuntimeError("State mismatch — possible CSRF attack")
        code = code_holder.get("code")
        if not code:
            raise RuntimeError("No authorization code received from browser callback")

        return await self._exchange_code(code, redirect_uri)

    async def _exchange_code(self, code: str, redirect_uri: str) -> TokenInfo:
        body = {
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": self._config.oauth_app_secret,
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": code,
            "redirect_uri": redirect_uri,
        }
        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.post(_TOKEN_URL, data=body)
        if resp.status_code >= 400:
            raise httpx.HTTPError(f"Token exchange failed: {resp.status_code}\n{resp.text}")
        return self._parse_token(resp.json())

    async def _refresh_token(self, refresh_token: str) -> TokenInfo | None:
        body = {
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": self._config.oauth_app_secret,
            "grant_type": "refresh_token",
            "assertion": refresh_token,
            "redirect_uri": "http://localhost:0/callback",
        }
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                resp = await http.post(_TOKEN_URL, data=body)
            if resp.status_code >= 400:
                self._log.warning("token refresh failed", status=resp.status_code)
                return None
            self._log.info("token refreshed successfully")
            return self._parse_token(resp.json())
        except Exception as exc:  # noqa: BLE001
            self._log.warning("token refresh failed", error=str(exc))
            return None

    @staticmethod
    def _parse_token(payload: dict) -> TokenInfo:
        expires_in = int(payload.get("expires_in", 3600))
        return TokenInfo(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in - 60),
        )

    def _load_token(self) -> TokenInfo | None:
        if not self._token_file.exists():
            return None
        try:
            return TokenInfo.from_json(self._token_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def _save_token(self, token: TokenInfo) -> None:
        self._token_file.write_text(token.to_json(), encoding="utf-8")
        # The file holds the long-lived refresh + access token. Restrict it to the
        # owner (0600) so other local users can't read it. Best-effort: on Windows
        # POSIX chmod is a no-op for group/other bits, but harmless.
        with contextlib.suppress(OSError):
            os.chmod(self._token_file, 0o600)
        self._log.debug("token saved", path=str(self._token_file))


def _make_callback_handler(holder: dict[str, str]) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            params = parse_qs(urlparse(self.path).query)
            holder["code"] = params.get("code", [""])[0]
            holder["state"] = params.get("state", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Login successful!</h2>"
                b"<p>You can close this tab.</p></body></html>"
            )

        def log_message(self, *args) -> None:  # silence default logging
            pass

    return _Handler
