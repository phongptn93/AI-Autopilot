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


_USAGE = """usage: ai-autopilot [doctor | pr-doctor <url> | evals [dir] [--min-pass-rate R]]

  (no argument)  start the autopilot (poller, PR babysitter, dashboard, webhooks)
  doctor         audit the configuration for coherence and exit
  pr-doctor URL  say why a comment on that pull request did not reach the autopilot
  evals [dir]    run the agent-configuration eval suite (default dir: evals/) and exit
                 non-zero when the pass rate is under --min-pass-rate (default 1.0)
"""


def _run_evals(args: list[str]) -> int:
    """``ai-autopilot evals`` — run the suite and return a shell exit code.

    Threshold defaults to 1.0. A suite whose whole purpose is to catch a regression
    should start by demanding no regression at all; a team that needs slack can say so
    explicitly, which is a decision worth having on the command line where it is read.
    """
    import asyncio as _asyncio

    from ai_autopilot import evals as evals_mod
    from ai_autopilot.config import load_settings

    directory, threshold = "evals", 1.0
    rest = list(args)
    if rest and not rest[0].startswith("-"):
        directory = rest.pop(0)
    for i, arg in enumerate(rest):
        if arg == "--min-pass-rate" and i + 1 < len(rest):
            try:
                threshold = float(rest[i + 1])
            except ValueError:
                print(f"not a number: {rest[i + 1]}", file=sys.stderr)
                return 2

    cases = evals_mod.load_cases(directory)
    if not cases:
        print(f"No eval cases under {directory!r} — nothing to prove.", file=sys.stderr)
        return 1
    config = load_settings()
    result = _asyncio.run(
        evals_mod.run_suite(cases, evals_mod.claude_runner(config))
    )
    print(evals_mod.format_report(result, threshold))
    return 0 if result.pass_rate >= threshold else 1


def main() -> None:
    # One subcommand, so argparse would be more machinery than it is worth. `doctor` must
    # work without starting anything: it is what you run when the autopilot is NOT
    # behaving, so it must not need a working autopilot.
    argv = sys.argv[1:]
    if argv:
        if argv[0] in ("doctor", "--doctor"):
            from ai_autopilot import doctor

            sys.exit(doctor.run())
        if argv[0] in ("pr-doctor", "--pr-doctor"):
            # Same spirit as `doctor`: you run it when the bot is NOT responding, so it
            # must not need a running autopilot — just the config and the PAT.
            from ai_autopilot import pr_doctor

            sys.exit(pr_doctor.run(argv[1] if len(argv) > 1 else ""))
        if argv[0] in ("evals", "--evals"):
            # Same spirit as `doctor`: it must run without a live autopilot, because it
            # is what CI calls on a change to the skills and rules that steer the agent.
            sys.exit(_run_evals(argv[1:]))
        if argv[0] in ("-h", "--help", "help"):
            print(_USAGE)
            sys.exit(0)
        print(_USAGE, file=sys.stderr)
        sys.exit(2)

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
