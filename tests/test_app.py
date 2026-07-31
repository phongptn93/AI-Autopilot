"""End-to-end smoke tests for the FastAPI app (routes, dashboard, webhook).

Runs with no ADO auth, so the poller idles immediately and nothing touches the
network except the ADO health check (which fails gracefully → 503).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ai_autopilot import dashboard
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.dashboard import settings_form


@pytest.fixture
def client(tmp_path) -> TestClient:
    settings = Settings(
        dry_run=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
    )
    with TestClient(create_app(settings)) as c:
        yield c


def test_metrics_endpoint(client: TestClient):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "autopilot_tasks_total" in resp.text


@pytest.mark.parametrize(
    "path",
    [
        "/dashboard",
        "/dashboard/board",
        "/dashboard/planning",
        "/dashboard/history",
        "/dashboard/config",
        "/dashboard/capabilities",
        "/dashboard/settings",
        "/dashboard/analytics",
        "/dashboard/queue",
        "/dashboard/audit",
    ],
)
def test_dashboard_pages_render(client: TestClient, path: str):
    resp = client.get(path)
    assert resp.status_code == 200
    assert "AI Autopilot" in resp.text


def test_settings_post_persists_and_applies(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(cfg_file))
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    with TestClient(create_app(settings)) as client:
        resp = client.post(
            "/dashboard/settings",
            data={"trigger_tag": "deploy-me", "workspace_directory": "/ws"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # Applied live on the running container.
        assert client.app.state.container.config.trigger_tag == "deploy-me"
    # Persisted to the YAML file.
    import yaml

    saved = yaml.safe_load(cfg_file.read_text())
    assert saved["trigger_tag"] == "deploy-me"
    assert saved["workspace_directory"] == "/ws"


def test_export_config_omits_pat(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        ado_pat="top-secret", ado_project="ExportProj",
    )
    with TestClient(create_app(settings)) as client:
        resp = client.get("/dashboard/settings/export")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert "ExportProj" in resp.text
    assert "top-secret" not in resp.text          # PAT never exported
    assert "ado_pat" not in resp.text


def test_export_full_config_is_encrypted_and_round_trips(tmp_path):
    from ai_autopilot import security

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        ado_pat="top-secret", ado_project="ExportProj", config_export_password="pw-xyz",
    )
    with TestClient(create_app(settings)) as client:
        resp = client.get("/dashboard/settings/export-full")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert b"top-secret" not in resp.content            # ciphertext must not leak the PAT
    decrypted = security.decrypt_bytes(resp.content, "pw-xyz").decode("utf-8")
    assert "top-secret" in decrypted                    # PAT IS in the encrypted payload
    assert "ExportProj" in decrypted


def test_import_full_restores_secrets(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(cfg_file))
    # A source config with a secret, exported + encrypted (as from another machine).
    blob = settings_form.export_full_encrypted(
        Settings(ado_pat="restore-me", ado_project="RP"), "kpw"
    )
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    with TestClient(create_app(settings)) as client:
        resp = client.post(
            "/dashboard/settings/import-full",
            data={"password": "kpw"},
            files={"file": ("autopilot-config-full.enc", blob, "application/octet-stream")},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/dashboard/settings"
        assert resp.cookies["autopilot_flash"] == "imported_full"
        cfg = client.app.state.container.config
        assert cfg.ado_pat == "restore-me"      # secret WAS restored (unlike safe import)
        assert cfg.ado_project == "RP"
    import yaml

    saved = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert saved["ado_pat"] == "restore-me"     # persisted to the config file


def test_import_full_wrong_password_reports_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))
    blob = settings_form.export_full_encrypted(Settings(ado_pat="x"), "right")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    with TestClient(create_app(settings)) as client:
        resp = client.post(
            "/dashboard/settings/import-full",
            data={"password": "wrong"},
            files={"file": ("c.enc", blob, "application/octet-stream")},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.cookies["autopilot_flash"] == "err_wrong_password"
        assert client.app.state.container.config.ado_pat == ""  # nothing applied


def test_dashboard_auth_gate(tmp_path):
    from ai_autopilot import security

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        dashboard_auth_password_hash=security.hash_password("s3cret"),
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/dashboard").status_code == 401           # no credentials
        assert client.get("/dashboard", auth=("x", "wrong")).status_code == 401
        assert client.get("/dashboard", auth=("x", "s3cret")).status_code == 200
        # health/metrics stay open for probes even when the dashboard is locked
        assert client.get("/health").status_code in (200, 503)


def test_import_config_applies_without_pat(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(cfg_file))
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    shared = "ado_project: TeamProj\nresolved_state: Closed\nado_pat: should-be-ignored\n"
    with TestClient(create_app(settings)) as client:
        resp = client.post(
            "/dashboard/settings/import",
            files={"file": ("autopilot-config.yaml", shared, "application/x-yaml")},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        cfg = client.app.state.container.config
        assert cfg.ado_project == "TeamProj"        # applied live
        assert cfg.resolved_state == "Closed"
        assert cfg.ado_pat == ""                     # PAT not imported
    import yaml

    saved = yaml.safe_load(cfg_file.read_text())
    assert "ado_pat" not in saved                    # PAT never written


def test_board_move_applies_tag_exclusively(tmp_path):
    from ai_autopilot.models import WorkItemInfo

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        board_drop_map=["Done => autopilot-done", "In review => autopilot-review"],
    )

    class _FakeAdo:
        def __init__(self):
            self.added, self.removed, self.states = [], [], []

        async def get_work_item(self, i):
            return WorkItemInfo(id=i, tags=["autopilot-review"])  # currently in review

        async def add_tag(self, i, t):
            self.added.append((i, t))

        async def remove_tag(self, i, t):
            self.removed.append((i, t))

        async def update_state(self, i, s):
            self.states.append((i, s))

    with TestClient(create_app(settings)) as client:
        fake = _FakeAdo()
        client.app.state.container.ado = fake
        resp = client.post("/dashboard/board/move", data={"item_id": "5", "column": "Done"})
        assert resp.status_code == 204
        assert (5, "autopilot-done") in fake.added
        assert (5, "autopilot-review") in fake.removed   # other drop-tag cleared

        # a column not in the map → no-op
        fake.added.clear()
        resp = client.post("/dashboard/board/move", data={"item_id": "5", "column": "Queued"})
        assert resp.status_code == 204 and fake.added == []


def test_queue_resume_no_ids_redirects(client: TestClient):
    resp = client.post("/dashboard/queue/resume", data={}, follow_redirects=False)
    assert resp.status_code == 303 and "/dashboard/queue" in resp.headers["location"]


def test_settings_save_writes_audit_event(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    with TestClient(create_app(settings)) as client:
        client.post("/dashboard/settings", data={"trigger_tag": "t"}, follow_redirects=False)
        page = client.get("/dashboard/audit")
        assert page.status_code == 200
        assert "config.updated" in page.text          # the save landed in the trail


def test_planning_page_shows_wave_and_reasons(client: TestClient):
    from datetime import UTC, datetime

    client.app.state.container.scheduler_view = {
        "at": datetime.now(UTC),
        "candidates": 2,
        "ready": [
            {"id": 10, "title": "Build A", "category": "BackendTask",
             "priority": 2, "predecessors": [], "related": [11]},
        ],
        "deferred": [
            {"id": 11, "title": "Build B", "reason": "xung đột (Related) với #10 — chạy wave sau"},
        ],
    }
    resp = client.get("/dashboard/planning")
    assert resp.status_code == 200
    assert "#10" in resp.text                 # ready item id in the live-schedule panel
    assert "Build B" in resp.text             # deferred item title
    assert "xung đột" in resp.text            # deferred reason


def test_planning_start_tags_and_sets_state(tmp_path):
    from ai_autopilot.models import WorkItemInfo

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        trigger_tag="autopilot", trigger_states=["Active"], planning_start_state="Active",
    )

    class _FakeAdo:
        def __init__(self):
            self.tags, self.states = [], []

        async def get_work_item(self, i):
            return WorkItemInfo(id=i, state="Closed", tags=[])       # not in a trigger state

        async def add_tag(self, i, t):
            self.tags.append((i, t))

        async def update_state(self, i, s):
            self.states.append((i, s))

    with TestClient(create_app(settings)) as client:
        fake = _FakeAdo()
        client.app.state.container.ado = fake
        resp = client.post(
            "/dashboard/planning/start",
            data={"ids": ["5", "6"], "mode": "now"},
            follow_redirects=False,
        )
        assert resp.status_code == 303 and "started=2" in resp.headers["location"]
        assert (5, "autopilot") in fake.tags and (5, "Active") in fake.states


def test_planning_start_preserves_filter_on_redirect(tmp_path):
    from ai_autopilot.models import WorkItemInfo

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        trigger_tag="autopilot", trigger_states=["Active"], planning_start_state="Active",
    )

    class _FakeAdo:
        async def get_work_item(self, i):
            return WorkItemInfo(id=i, state="Closed", tags=[])

        async def add_tag(self, i, t):
            pass

        async def update_state(self, i, s):
            pass

    with TestClient(create_app(settings)) as client:
        client.app.state.container.ado = _FakeAdo()
        resp = client.post(
            "/dashboard/planning/start",
            data={
                "ids": ["5"], "mode": "now",
                "assignee": "alice@example.com", "state": "Active", "type": "Bug",
            },
            follow_redirects=False,
        )
        loc = resp.headers["location"]
        assert resp.status_code == 303
        assert "assignee=alice%40example.com" in loc
        assert "state=Active" in loc and "type=Bug" in loc and "started=1" in loc


def test_planning_filter_remembered_via_cookie(tmp_path):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")

    class _FakeAdo:
        async def get_work_items_by_assignee(self, assignee, states=None, types=None, top=200):
            return []

    with TestClient(create_app(settings)) as client:
        client.app.state.container.ado = _FakeAdo()
        # Explicit filter → rendered + stored in a cookie.
        r1 = client.get("/dashboard/planning?assignee=alice@x.com&state=Active&type=Bug")
        assert r1.status_code == 200 and "alice@x.com" in r1.text
        assert client.cookies.get("planning_filter")
        # No params → restored from the cookie (the client jar re-sends it).
        r2 = client.get("/dashboard/planning")
        assert "alice@x.com" in r2.text and "Active" in r2.text


def test_board_filters_and_limit(tmp_path):
    from datetime import datetime

    from ai_autopilot.models import WorkItemInfo

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}", board_max_per_column=20
    )

    class _FakeAdo:
        async def get_all_tagged_work_items(self):
            items = [
                WorkItemInfo(id=i, title=f"[BE] task {i}", state="Active",
                             tags=["phong-autopilot"], changed_date=datetime(2026, 7, 1))
                for i in range(1, 31)
            ]
            items.append(WorkItemInfo(id=99, title="[FE] special login", state="Active",
                                      tags=["phong-autopilot"], changed_date=datetime(2026, 7, 10)))
            return items

    with TestClient(create_app(settings)) as client:
        client.app.state.container.ado = _FakeAdo()
        # Cap: 31 BE items in one column > 20 → a Load more appears.
        assert "Load more" in client.get("/dashboard/board").text
        # limit override removes it.
        assert "Load more" not in client.get("/dashboard/board?limit=100").text
        # Category filter keeps only FE.
        fe = client.get("/dashboard/board?cat=FE").text
        assert "special login" in fe and "task 5" not in fe
        # Changed-date filter keeps only the recent one.
        dated = client.get("/dashboard/board?from=2026-07-05").text
        assert "special login" in dated and "task 5" not in dated
        # Search by title.
        assert "special login" in client.get("/dashboard/board?q=login").text


def test_planning_live_partial_renders(tmp_path):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    with TestClient(create_app(settings)) as client:
        r = client.get("/dashboard/planning/live-partial")
        assert r.status_code == 200 and "Live schedule" in r.text


def test_planning_schedule_creates_a_run(tmp_path):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    with TestClient(create_app(settings)) as client:
        resp = client.post(
            "/dashboard/planning/start",
            data={"ids": ["7"], "mode": "schedule", "when_at": "2099-01-01T08:00"},
            follow_redirects=False,
        )
        assert resp.status_code == 303 and "scheduled=1" in resp.headers["location"]
        page = client.get("/dashboard/planning")
        assert "Scheduled runs" in page.text


def test_overview_shows_efficiency_cards(client: TestClient):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Merge Rate" in resp.text
    assert "Tokens / Merged PR" in resp.text
    assert "Runs / Item" in resp.text


def test_health_reports_checks(client: TestClient):
    resp = client.get("/health")
    body = resp.json()
    names = {c["name"] for c in body["checks"]}
    assert names == {"ado", "claude", "disk"}


def test_webhook_enqueues(client: TestClient):
    resp = client.post("/api/webhook/ado", json={"resource": {"workItemId": 123}})
    assert resp.status_code == 200
    assert resp.json() == {"queued": 123}


def test_webhook_rejects_missing_id(client: TestClient):
    resp = client.post("/api/webhook/ado", json={"resource": {}})
    assert resp.json() == {"error": "No workItemId in payload"}


class _FakeMonitor:
    def __init__(self):
        self.kicks = []

    def kick(self, repo_id, repo_name, pr):
        self.kicks.append((repo_id, repo_name, pr.get("pullRequestId")))


def _pr_comment_event(content: str) -> dict:
    return {
        "eventType": "ms.vss-code.git-pullrequest-comment-event",
        "resource": {
            "comment": {"content": content},
            "pullRequest": {
                "pullRequestId": 7,
                "sourceRefName": "refs/heads/feature/be/42-x",
                "repository": {"id": "repo-guid", "name": "repo-a"},
            },
        },
    }


def test_webhook_pr_command_kicks_monitor(client: TestClient):
    fake = _FakeMonitor()
    client.app.state.pr_monitor = fake
    resp = client.post("/api/webhook/ado", json=_pr_comment_event("/ai fix issue 2"))
    assert resp.json() == {"kicked": 7}
    assert fake.kicks == [("repo-guid", "repo-a", 7)]


def test_webhook_ignores_bot_and_chatter(client: TestClient):
    from ai_autopilot.config import BOT_COMMENT_PREFIX

    fake = _FakeMonitor()
    client.app.state.pr_monitor = fake
    # The bot's own reply fires the hook too — must never self-trigger.
    r1 = client.post("/api/webhook/ado", json=_pr_comment_event(BOT_COMMENT_PREFIX + "done"))
    # Human chatter without a /command → the regular poll ignores it anyway.
    r2 = client.post("/api/webhook/ado", json=_pr_comment_event("nice work!"))
    assert r1.json() == {"ignored": "bot comment"}
    assert r2.json() == {"ignored": "not a command"}
    assert fake.kicks == []


# ── Login page (replaces the browser's Basic-auth popup) ──────────────────────

_HTML = {"accept": "text/html,application/xhtml+xml"}


def _locked_app(tmp_path, password="s3cret"):
    from ai_autopilot import security
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        dashboard_auth_password_hash=security.hash_password(password),
    )


def test_browser_is_redirected_to_the_login_page_not_a_popup(tmp_path):
    """`WWW-Authenticate: Basic` is exactly what makes the browser show its native
    credential popup, so an HTML request must not receive it."""
    with TestClient(create_app(_locked_app(tmp_path))) as client:
        resp = client.get("/dashboard/board", headers=_HTML, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/dashboard/login?next=%2Fdashboard%2Fboard"
        assert "www-authenticate" not in resp.headers


def test_non_html_callers_still_get_a_plain_401(tmp_path):
    """curl / probes / scripts cannot render a login page; they keep the machine-readable
    answer, and Basic auth keeps working for them."""
    with TestClient(create_app(_locked_app(tmp_path))) as client:
        resp = client.get("/dashboard", follow_redirects=False)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"].startswith("Basic")
        assert client.get("/dashboard", auth=("x", "s3cret")).status_code == 200


def test_login_page_is_reachable_while_locked(tmp_path):
    with TestClient(create_app(_locked_app(tmp_path))) as client:
        resp = client.get("/dashboard/login", headers=_HTML)
        assert resp.status_code == 200
        assert 'name="password"' in resp.text
        assert 'name="next"' in resp.text


def test_wrong_password_reports_401_and_sets_no_cookie(tmp_path):
    from ai_autopilot import security
    with TestClient(create_app(_locked_app(tmp_path))) as client:
        resp = client.post("/dashboard/login", data={"password": "nope", "next": "/dashboard"},
                           headers=_HTML, follow_redirects=False)
        assert resp.status_code == 401           # a failed login is not a page view
        assert "Mật khẩu không đúng" in resp.text
        assert security.SESSION_COOKIE not in resp.cookies


def test_correct_password_sets_a_session_that_grants_access(tmp_path):
    from ai_autopilot import security
    with TestClient(create_app(_locked_app(tmp_path))) as client:
        resp = client.post("/dashboard/login",
                           data={"password": "s3cret", "next": "/dashboard/board"},
                           headers=_HTML, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/dashboard/board"
        assert client.cookies.get(security.SESSION_COOKIE)
        # The cookie alone is now enough — no Authorization header.
        assert client.get("/dashboard", headers=_HTML).status_code == 200


def test_next_cannot_be_used_as_an_open_redirect(tmp_path):
    """`next` is attacker-controllable. `//evil.example` is a scheme-relative URL that a
    browser follows off-site, yet it still "starts with a slash"."""
    with TestClient(create_app(_locked_app(tmp_path))) as client:
        for hostile in ("//evil.example", "https://evil.example", "/etc/passwd",
                        "/dashboard//evil.example"):
            resp = client.post("/dashboard/login",
                               data={"password": "s3cret", "next": hostile},
                               headers=_HTML, follow_redirects=False)
            assert resp.headers["location"] == "/dashboard", hostile


def test_logout_ends_the_session(tmp_path):
    with TestClient(create_app(_locked_app(tmp_path))) as client:
        client.post("/dashboard/login", data={"password": "s3cret", "next": "/dashboard"},
                    headers=_HTML, follow_redirects=False)
        assert client.get("/dashboard", headers=_HTML).status_code == 200
        out = client.post("/dashboard/logout", headers=_HTML, follow_redirects=False)
        assert out.status_code == 303 and out.headers["location"] == "/dashboard/login"
        again = client.get("/dashboard", headers=_HTML, follow_redirects=False)
        assert again.status_code == 303 and "login" in again.headers["location"]


def test_login_page_steps_aside_when_no_password_is_set(tmp_path):
    """With the dashboard open, a login form would be a control that authenticates nothing."""
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    with TestClient(create_app(settings)) as client:
        resp = client.get("/dashboard/login", headers=_HTML, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/dashboard"


# ── Full export must refuse to produce an unprotected file ────────────────────

def test_full_export_is_refused_without_an_export_password(tmp_path):
    """Encrypting under "" yields a valid-looking .enc whose key anyone can reproduce —
    the PAT and every token would ship with no real protection, while looking protected."""
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        ado_pat="a-real-pat", config_export_password="",
    )
    with TestClient(create_app(settings)) as client:
        resp = client.get("/dashboard/settings/export-full", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.cookies["autopilot_flash"] == "err_no_export_password"
        # The refusal must name the setting to fix, not just say "no".
        colour, message = dashboard.FLASH_MESSAGES["err_no_export_password"]
        assert colour == "red" and "config_export_password" in message


def test_full_export_works_once_a_password_is_set(tmp_path):
    from ai_autopilot import security
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
        ado_pat="a-real-pat", config_export_password="pw",
    )
    with TestClient(create_app(settings)) as client:
        resp = client.get("/dashboard/settings/export-full")
        assert resp.status_code == 200
        assert b"a-real-pat" not in resp.content              # encrypted, not plaintext
        assert b"a-real-pat" in security.decrypt_bytes(resp.content, "pw")


# ── Outcome banners travel in a one-shot cookie, never in the URL ─────────────

def test_saving_settings_reports_the_outcome_without_putting_it_in_the_url(tmp_path, monkeypatch):
    """A `?saved=1` redirect makes the banner part of the address: refreshing replays
    "saved" without saving, and the URL can be shared showing a save that never happened."""
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    with TestClient(create_app(settings)) as client:
        resp = client.post(
            "/dashboard/settings", data={"ado_project": "Flashy"}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/dashboard/settings"   # nothing in the query
        assert resp.cookies["autopilot_flash"] == "saved"

        # The banner shows once…
        page = client.get("/dashboard/settings")
        assert "Đã lưu và áp dụng" in page.text
        # …and the GET clears the cookie, so a refresh is silent.
        assert client.cookies.get("autopilot_flash") in (None, "")
        assert "Đã lưu và áp dụng" not in client.get("/dashboard/settings").text


def test_every_flash_redirect_uses_a_code_that_has_wording(tmp_path):
    """_flash() with an unknown code sets no cookie, so the outcome would vanish
    silently. Catch a typo'd code at test time rather than in a user's browser."""
    import re

    source = (Path(dashboard.__file__)).read_text(encoding="utf-8")
    used = set(re.findall(r'_flash\(\s*"[^"]*"\s*,\s*"([^"]+)"', source))
    assert used, "no _flash() call sites found — did the redirects move?"
    assert used <= set(dashboard.FLASH_MESSAGES), used - set(dashboard.FLASH_MESSAGES)
