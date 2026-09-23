"""Is there a newer release, and can this machine take it?

The machine already knew it was out of date and could do nothing about it: a worker's
only signal was one log line and a shell command printed on a page for somebody to copy.
This module answers the two questions that turns into an action — *is there a newer
build* and *is this the kind of install that can apply one* — and nothing else. Applying
it lives in the dashboard route, because it needs the poller, the audit log and the
process.

Deliberately blunt about what it will NOT do:

* it never installs on its own (a bad release must not be able to walk itself across a
  fleet while nobody is watching);
* it refuses an editable checkout, where ``pip install --upgrade`` fights the working
  tree instead of updating it; and
* it refuses inside a container, where the image is the unit of deployment and anything
  pip writes evaporates at the next restart.

Everything here fails soft. A rate-limited or unreachable GitHub means "no news", never
a broken page and never a broken poll cycle.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from ai_autopilot import fleet
from ai_autopilot.logging_config import describe_exc, get_logger

_log = get_logger("updates")

#: Unauthenticated GitHub allows 60 requests per hour per IP, so this is asked on a slow
#: timer and cached — never per page load.
_API = "https://api.github.com/repos/{repo}/releases/latest"
_TIMEOUT = 20.0

# Why this machine cannot apply an update, when it cannot. Kept as codes so the page can
# say the right sentence and a test can assert on the reason rather than on prose.
BLOCK_EDITABLE = "editable"
BLOCK_CONTAINER = "container"


@dataclass(frozen=True)
class Release:
    """A published release, reduced to what applying it needs."""

    version: str = ""
    wheel_url: str = ""
    notes_url: str = ""
    published_at: str = ""

    @property
    def installable(self) -> bool:
        """A release with no wheel cannot be applied, whatever its version says."""
        return bool(self.version and self.wheel_url)


#: "Caller said nothing" — distinct from "caller said there is no checkout". Without the
#: distinction, ``install_block(workspace_root=None)`` meant *look it up*, so a caller
#: stating plainly that this is a wheel install got the opposite answer.
_LOOK_IT_UP = object()


def install_block(workspace_root: Path | None | object = _LOOK_IT_UP) -> str:
    """Why this install cannot self-update, or '' when it can.

    ``workspace_root`` is the package's own source checkout: a ``Path`` for an editable
    install, ``None`` for a wheel one. Left out, it is looked up through
    :mod:`ai_autopilot.doctor`, whose ``_repo_root`` already answers exactly this
    question for the version check ("the source checkout this package lives in, or None
    when installed as a wheel") and is the one place that should know.
    """
    if os.path.exists("/.dockerenv") or os.environ.get("KUBERNETES_SERVICE_HOST"):
        # The image is the unit here. `pip install` inside a running container changes a
        # filesystem layer that the next restart throws away — so it would report success
        # and silently revert, which is worse than refusing.
        return BLOCK_CONTAINER
    if workspace_root is _LOOK_IT_UP:
        from ai_autopilot.doctor import _repo_root

        workspace_root = _repo_root()
    if workspace_root is not None:
        # `pip install -e .` points at this tree; upgrading from a wheel would leave two
        # installs disagreeing about which code is running. `git pull` is the update.
        return BLOCK_EDITABLE
    return ""


def newer(current: str, latest: str) -> bool:
    """Is ``latest`` a newer build than ``current``?

    Reuses :func:`ai_autopilot.fleet.is_behind`, which the fleet page already compares
    versions with — a second semver parser in the same codebase is a second set of edge
    cases to get wrong.
    """
    return fleet.is_behind(current, latest)


def _pick(payload: dict) -> Release:
    """One GitHub release payload as a :class:`Release`.

    The wheel is taken from the assets the release actually carries, never assembled
    from a naming convention: the file name is a build detail that has changed before,
    and a URL guessed from a pattern fails at download time — after the drain, with the
    poller already stopped.
    """
    tag = str(payload.get("tag_name") or "").strip()
    version = tag[1:] if tag[:1].lower() == "v" else tag
    wheel = ""
    for asset in payload.get("assets") or []:
        name = str((asset or {}).get("name") or "")
        if name.endswith(".whl"):
            wheel = str(asset.get("browser_download_url") or "")
            break
    return Release(
        version=version,
        wheel_url=wheel,
        notes_url=str(payload.get("html_url") or ""),
        published_at=str(payload.get("published_at") or ""),
    )


async def latest_release(client, repo: str) -> Release | None:
    """The newest published release, or ``None`` when it cannot be established.

    ``None`` means "no news" and is returned for every failure — offline, rate-limited,
    repo renamed, a body that is not the shape we expect. A machine that cannot reach
    GitHub is not a machine with a problem to report; it is a machine that should carry
    on working.
    """
    repo = (repo or "").strip().strip("/")
    if not repo or "/" not in repo:
        return None
    try:
        resp = await client.get(
            _API.format(repo=repo),
            headers={"Accept": "application/vnd.github+json"},
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            _log.info("update check: GitHub said no", repo=repo, status=resp.status_code)
            return None
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 — a check must never break the caller
        _log.info("update check failed", repo=repo, error=describe_exc(exc))
        return None
    if not isinstance(payload, dict):
        return None
    release = _pick(payload)
    return release if release.version else None


def upgrade_command(release: Release) -> str:
    """The command a human would run — shown when this machine may not run it itself."""
    if not release.installable:
        return ""
    return f'"{sys.executable}" -m pip install --upgrade {release.wheel_url}'
