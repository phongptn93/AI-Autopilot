"""CLI entry point: ``python -m ai_autopilot`` / ``ai-autopilot``."""

from __future__ import annotations

import getpass
import sys

import uvicorn

from ai_autopilot import security
from ai_autopilot.config import config_file_path, load_settings
from ai_autopilot.dashboard import settings_form


def _ensure_dashboard_password() -> None:
    """First-run bootstrap: make sure the dashboard is password-protected.

    If neither a password hash nor the legacy token is configured, prompt for a
    password on the console and persist its PBKDF2 hash to config.yaml so it
    applies on this and every later start. When there is no interactive terminal
    (Docker / systemd), we do NOT block startup — instead we warn and let the
    operator set ``AUTOPILOT_DASHBOARD_AUTH_PASSWORD_HASH`` / ``_TOKEN`` (or bind
    to loopback). ``create_app`` re-reads config, so the saved hash takes effect.
    """
    config = load_settings()
    if config.dashboard_auth_password_hash or config.dashboard_auth_token:
        return

    if not sys.stdin.isatty():
        print(
            "WARNING: the dashboard has NO password set and no interactive terminal "
            "is available to set one. Anyone who can reach it can read/rewrite your "
            "config (incl. the ADO PAT). Set AUTOPILOT_DASHBOARD_AUTH_PASSWORD_HASH "
            "(or the legacy AUTOPILOT_DASHBOARD_AUTH_TOKEN), or keep health_host on "
            "127.0.0.1.",
            file=sys.stderr,
        )
        return

    print("No dashboard password is set yet. Set one now to protect the settings UI.")
    for _ in range(3):
        first = getpass.getpass("New dashboard password: ")
        if not first.strip():
            print("  Password cannot be empty — try again.")
            continue
        if first != getpass.getpass("Confirm password: "):
            print("  Passwords did not match — try again.")
            continue
        settings_form.save_to_yaml(
            config_file_path(),
            {"dashboard_auth_password_hash": security.hash_password(first)},
        )
        print(f"  Saved. The dashboard now requires this password (hash in {config_file_path()}).")
        return
    print("  Giving up after 3 attempts — starting WITHOUT a dashboard password.", file=sys.stderr)


def main() -> None:
    _ensure_dashboard_password()
    config = load_settings()
    uvicorn.run(
        "ai_autopilot.app:create_app",
        factory=True,
        host=config.health_host,
        port=config.health_port,
        log_config=None,  # logging is configured inside create_app()
    )


if __name__ == "__main__":
    main()
