"""Tests for the per-process board lenses (pure model + editor form parsing)."""

from __future__ import annotations

from ai_autopilot.board import BoardCard, board_columns, build_board
from ai_autopilot.config import Settings
from ai_autopilot.lenses import (
    DEFAULT_LENSES,
    board_view,
    board_views,
    coverage_gaps,
    group_lanes,
    in_lens,
    lens_tag_matches,
    parse_lens_form,
    render_lanes,
    validate_lenses,
)
from ai_autopilot.models import WorkItemInfo

CFG = Settings()
COLS = board_columns(CFG)

# The shipped processes are a relay and carry no tags. Tag routing is the other,
# opt-in shape — one process owning a whole stream — so it gets its own config.
TAGGED = Settings(board_lenses=[
    {"key": "qc", "label": "QC", "tags": ["autopilot-qc", "qc"],
     "stages": [{"name": "Testing", "columns": ["In review", "Queued"], "mine": True},
                {"name": "Passed", "columns": ["Done"]}]},
    {"key": "dev", "label": "Dev", "tags": ["autopilot-dev", "dev"],
     "stages": [{"name": "Building", "columns": ["Queued", "In progress"], "mine": True}]},
])


class _Form(dict):
    """The bits of starlette's FormData the parser uses."""

    def getlist(self, key):
        val = self.get(key)
        if val is None:
            return []
        return val if isinstance(val, list) else [val]


def _item(item_id: int, tags: list[str], state: str = "Active") -> WorkItemInfo:
    return WorkItemInfo(id=item_id, title=f"t{item_id}", work_item_type="Task",
                        state=state, tags=tags)


def test_defaults_give_pipeline_plus_three_processes():
    views = board_views(CFG)
    assert [v.key for v in views] == ["pipeline", "ba", "dev", "qc"]
    assert views[0].lanes[0].name == "Queued"  # pipeline is 1:1 with the columns


def test_unknown_view_key_falls_back_to_pipeline():
    assert board_view(CFG, "nope").key == "pipeline"
    assert board_view(CFG, None).key == "pipeline"
    assert board_view(CFG, "QC").key == "qc"  # slugged, case-insensitive


def test_lens_prunes_columns_this_config_does_not_have():
    # No board_deploy_state → no "Ready for deploy" column, so the QC stage over it goes.
    qc = board_view(CFG, "qc")
    assert "Ready for deploy" not in [lane.name for lane in qc.lanes]
    for lane in qc.lanes:
        assert set(lane.columns) <= set(COLS)


def test_lens_keeps_every_column_when_configured():
    cfg = Settings(board_review_state="Ready for Review",
                   board_testing_state="Ready for Testing",
                   board_deploy_state="Ready for Deploy")
    dev = board_view(cfg, "dev")
    covered = {c for lane in dev.lanes for c in lane.columns}
    assert covered == set(board_columns(cfg))


def test_drop_target_stays_inside_the_lane():
    for view in board_views(CFG):
        for lane in view.lanes:
            # A lane over ADO states only has no column to drop onto, and says so.
            assert lane.drop in lane.columns if lane.columns else lane.drop == ""


def test_tags_decide_which_items_a_process_owns():
    items = [
        _item(1, ["autopilot", "autopilot-qc"]),
        _item(2, ["autopilot", "autopilot-dev"]),
        _item(3, ["autopilot"]),
    ]
    board = build_board(items, {}, TAGGED)
    qc = board_view(TAGGED, "qc")
    ids = {c.id for cards in group_lanes(board, qc).values() for c in cards}
    assert ids == {1}
    # The pipeline view has no tags, so it keeps everything.
    pipeline = board_view(TAGGED, "pipeline")
    all_ids = {c.id for cards in group_lanes(board, pipeline).values() for c in cards}
    assert all_ids == {1, 2, 3}


def test_in_lens_is_case_insensitive_and_open_without_tags():
    qc = board_view(TAGGED, "qc")
    assert in_lens(BoardCard(1, "t", "Active", "Queued", tags=["Autopilot-QC"]), qc)
    assert not in_lens(BoardCard(2, "t", "Active", "Queued", tags=["other"]), qc)
    pipeline = board_view(CFG, "pipeline")
    assert in_lens(BoardCard(3, "t", "Active", "Queued", tags=[]), pipeline)


def test_lens_tag_matches_names_the_processes_for_a_card():
    labels = [v.label for v in lens_tag_matches(["qc", "dev"], board_views(TAGGED))]
    assert labels == ["QC", "Dev"]  # config order, pipeline never matches


def test_folded_lane_keeps_its_source_columns_apart():
    items = [
        _item(1, ["autopilot", "autopilot-dev", "autopilot-hold"]),  # escalated
        _item(2, ["autopilot", "autopilot-dev"]),                    # queued
    ]
    board = build_board(items, {}, CFG)
    dev = board_view(CFG, "dev")
    rows = {r.lane.name: r for r in render_lanes(dev, group_lanes(board, dev))}
    blocked = rows["Blocked"]
    assert blocked.total == 1
    assert [name for name, _ in blocked.groups] == ["Needs human"]


def test_render_lanes_caps_and_reports_the_remainder():
    items = [_item(i, ["autopilot", "autopilot-dev"]) for i in range(1, 6)]
    board = build_board(items, {}, CFG)
    dev = board_view(CFG, "dev")
    rows = {r.lane.name: r for r in render_lanes(dev, group_lanes(board, dev), limit=2)}
    assert rows["Backlog"].total == 5
    assert rows["Backlog"].hidden == 3
    assert sum(len(cards) for _, cards in rows["Backlog"].groups) == 2


def test_malformed_lens_entries_are_skipped_not_fatal():
    cfg = Settings(board_lenses=[
        {"key": "broken"},                       # no stages
        {"label": "Ops", "stages": [{"name": "All", "columns": ["Queued"]}]},
        {"key": "pipeline", "stages": [{"name": "x", "columns": ["Queued"]}]},  # reserved
    ])
    assert [v.key for v in board_views(cfg)] == ["pipeline", "ops"]


def test_parse_form_round_trips_a_process():
    form = _Form({
        "lens0_label": "Design",
        "lens0_key": "",                          # blank key → slugged from the label
        "lens0_icon": "🎨",
        "lens0_hint": "What needs a mock",
        "lens0_tags": "autopilot-design, design\ndesign-review",
        "lens0_stage0_name": "Waiting",
        "lens0_stage0_columns": ["Queued", "In progress"],
        "lens0_stage0_tone": "teal",
        "lens0_stage0_hint": "not drawn yet",
        "lens0_stage0_drop": "In progress",
    })
    parsed = parse_lens_form(form, COLS)
    assert len(parsed) == 1
    lens = parsed[0]
    assert lens["key"] == "design"
    assert lens["tags"] == ["autopilot-design", "design", "design-review"]
    assert lens["stages"][0]["columns"] == ["Queued", "In progress"]  # pipeline order
    assert lens["stages"][0]["drop"] == "In progress"
    assert validate_lenses(parsed, COLS) == []


def test_parse_form_drops_blank_rows_and_bad_values():
    form = _Form({
        "lens0_label": "",                        # no label → whole lens dropped
        "lens0_stage0_name": "x",
        "lens0_stage0_columns": ["Queued"],
        "lens1_label": "Ops",
        "lens1_stage0_name": "",                  # blank name → stage dropped
        "lens1_stage0_columns": ["Queued"],
        "lens1_stage1_name": "Live",
        "lens1_stage1_columns": ["Done"],
        "lens1_stage1_tone": "chartreuse",        # not a tone → slate
        "lens1_stage1_drop": "Queued",            # outside the stage → first column
    })
    parsed = parse_lens_form(form, COLS)
    assert [x["key"] for x in parsed] == ["ops"]
    assert len(parsed[0]["stages"]) == 1
    assert parsed[0]["stages"][0]["tone"] == "slate"
    assert parsed[0]["stages"][0]["drop"] == "Done"


def test_parse_form_drops_a_stage_whose_columns_are_all_unchecked():
    form = _Form({
        "lens0_label": "Ops",
        "lens0_stage0_name": "Nothing",           # no _columns key at all
        "lens0_stage1_name": "Live",
        "lens0_stage1_columns": ["Done"],
    })
    parsed = parse_lens_form(form, COLS)
    assert [st["name"] for st in parsed[0]["stages"]] == ["Live"]


def test_validate_rejects_duplicates_and_unknown_columns():
    errors = validate_lenses([
        {"key": "dev", "label": "Dev", "stages": [{"name": "A", "columns": ["Queued"]}]},
        {"key": "dev", "label": "Dev 2", "stages": [{"name": "B", "columns": ["Nowhere"]}]},
    ], COLS)
    assert any("duplicate" in e for e in errors)
    assert any("Nowhere" in e for e in errors)


def test_coverage_gaps_names_columns_no_stage_shows():
    lens = {"stages": [{"name": "A", "columns": ["Queued"]}]}
    gaps = coverage_gaps(lens, COLS)
    assert "Queued" not in gaps and "Done" in gaps
    # The shipped defaults cover every column of a default config.
    for default in DEFAULT_LENSES:
        assert coverage_gaps(default, COLS) == []


# ── Dashboard pages ──────────────────────────────────────────────────────────


def _client(tmp_path, **overrides):
    from starlette.testclient import TestClient

    from ai_autopilot.app import create_app

    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}", **overrides)
    return TestClient(create_app(settings))


def test_board_page_offers_a_tab_per_process(tmp_path):
    with _client(tmp_path) as client:
        page = client.get("/dashboard/board").text
        assert 'aria-label="Board view"' in page
        for key in ("ba", "dev", "qc"):
            assert f"view={key}" in page
        assert "Queued" in page               # the default view is the raw pipeline
        assert "/dashboard/board-views" in page   # the way to configure them


def test_board_lens_shows_only_its_own_items(tmp_path):
    class _FakeAdo:
        async def get_all_tagged_work_items(self):
            return [
                _item(1, ["autopilot", "autopilot-qc"]),
                _item(2, ["autopilot", "autopilot-dev"]),
            ]

    with _client(tmp_path, board_lenses=TAGGED.board_lenses) as client:
        client.app.state.container.ado = _FakeAdo()
        qc = client.get("/dashboard/board?view=qc").text
        assert 'data-id="1"' in qc and 'data-id="2"' not in qc
        assert "Testing" in qc                # a QC lane, not a pipeline column
        both = client.get("/dashboard/board").text
        assert 'data-id="1"' in both and 'data-id="2"' in both


def test_board_views_editor_round_trips(tmp_path, monkeypatch):
    import yaml

    cfg_file = tmp_path / "config.yaml"
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(cfg_file))
    with _client(tmp_path) as client:
        page = client.get("/dashboard/board-views")
        assert page.status_code == 200
        assert "autopilot-qc" in page.text     # defaults are shown for editing

        saved = client.post(
            "/dashboard/board-views",
            data={
                "lens0_label": "Release",
                "lens0_key": "release",
                "lens0_tags": "autopilot-release",
                "lens0_stage0_name": "Waiting",
                "lens0_stage0_columns": ["Queued", "In progress"],
                "lens0_stage0_tone": "teal",
                "lens0_stage0_drop": "Queued",
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
        cfg = client.app.state.container.config
        assert [v.key for v in board_views(cfg)] == ["pipeline", "release"]

        # A stage naming a column that does not exist is refused, not saved.
        bad = client.post(
            "/dashboard/board-views",
            data={
                "lens0_label": "Broken", "lens0_key": "broken",
                "lens0_stage0_name": "X", "lens0_stage0_columns": ["Queued"],
                "lens1_label": "Broken 2", "lens1_key": "broken",
                "lens1_stage0_name": "Y", "lens1_stage0_columns": ["Queued"],
            },
            follow_redirects=False,
        )
        assert bad.status_code == 303
        assert [v.key for v in board_views(client.app.state.container.config)] == [
            "pipeline", "release",
        ]

        # Reset restores the shipped set.
        client.post("/dashboard/board-views", data={"reset": "1"}, follow_redirects=False)
        assert [v.key for v in board_views(client.app.state.container.config)] == [
            "pipeline", "ba", "dev", "qc",
        ]

    written = yaml.safe_load(cfg_file.read_text())
    assert written["board_lenses"] == []


def test_shipped_processes_are_a_relay_not_a_set_of_labels():
    from ai_autopilot.lenses import default_lenses, my_turn_columns

    cfg = Settings(board_review_state="Ready for Review",
                   board_testing_state="Ready for Testing",
                   board_deploy_state="Ready for Deploy")
    assert all(not lens["tags"] for lens in default_lenses(cfg))   # nothing to re-tag
    turns = {v.key: my_turn_columns(v) for v in board_views(cfg) if v.key != "pipeline"}
    # The ball moves along the pipeline: BA holds intake, Dev the build, QC the checks.
    assert "Queued" in turns["ba"] and "In progress" not in turns["ba"]
    assert {"In progress", "Ready for deploy"} <= turns["dev"]
    assert turns["qc"] == {"Ready for testing"}   # review is Dev's once QC has a queue
    assert "Ready for review" in turns["dev"]
    # Deploying to the test env is Dev's move; QC only gets the ball once it is there.
    assert "Ready for deploy" not in turns["qc"]
    assert "Ready for testing" not in turns["dev"]
    assert "Queued" not in turns["qc"]


def test_a_lens_saved_under_the_old_column_name_still_renders():
    # A config written before the rename must keep working: the lane is what the
    # operator drew, and pruning an unknown column would empty it without a word.
    from ai_autopilot.lenses import lens_dicts, view_of

    cfg = Settings(board_deploy_state="Ready for Deploy", board_lenses=[{
        "key": "rel", "label": "Release",
        "stages": [{"name": "Shipping", "columns": ["Ready to deploy"],
                    "drop": "Ready to deploy", "mine": True}],
    }])
    assert lens_dicts(cfg)[0]["stages"][0]["columns"] == ["Ready for deploy"]
    lane = view_of(lens_dicts(cfg)[0], board_columns(cfg)).lanes[0]
    assert lane.columns == ("Ready for deploy",) and lane.drop == "Ready for deploy"


def test_suggested_role_tags_follow_this_instance_vocabulary():
    from ai_autopilot.lenses import role_tag_prefixes, suggested_role_tags

    cfg = Settings(trigger_tag="vm-autopilot")            # lifecycle tags left at default
    assert role_tag_prefixes(cfg) == ["autopilot-", "vm-autopilot-"]
    assert suggested_role_tags(cfg, "qc") == ["autopilot-qc", "vm-autopilot-qc", "qc"]

    # A team that renamed the lifecycle tags gets ITS family, not "autopilot-".
    renamed = Settings(trigger_tag="vm-autopilot", review_tag="ap-review",
                       processed_tag="ap-done", escalation_tag="ap-hold")
    assert role_tag_prefixes(renamed) == ["ap-", "vm-autopilot-"]
    assert suggested_role_tags(renamed, "dev") == ["ap-dev", "vm-autopilot-dev", "dev"]


def test_relay_hands_the_item_along_without_re_tagging(tmp_path):
    """One item, no role tag: whose turn it is follows where the item sits."""

    state = {"tags": ["vm-autopilot"], "state": "Active"}

    class _FakeAdo:
        async def get_all_tagged_work_items(self):
            return [WorkItemInfo(id=7, title="one item", work_item_type="Task",
                                 state=state["state"], tags=state["tags"])]

    import re

    with _client(tmp_path, trigger_tag="vm-autopilot",
                 board_review_state="Ready for Review",
                 board_deploy_state="Ready for Deploy") as client:
        client.app.state.container.ado = _FakeAdo()

        def waiting(view: str) -> int:
            """How many items that board says are waiting on it right now."""
            page = client.get(f"/dashboard/board?view={view}").text
            found = re.search(r"<b>(\d+)</b>\s*waiting on", page)
            return int(found.group(1)) if found else 0

        # Queued → BA's turn, and every board still SEES the item (relay, not silos).
        assert (waiting("ba"), waiting("dev"), waiting("qc")) == (1, 0, 0)
        for view in ("ba", "dev", "qc"):
            assert 'data-id="7"' in client.get(f"/dashboard/board?view={view}").text

        # The autopilot opens a PR and tags it for review — the one hand-off every
        # install has. That is QC's queue, and it is no longer BA's turn.
        state["tags"] = ["vm-autopilot", "autopilot-review"]
        assert (waiting("ba"), waiting("dev"), waiting("qc")) == (0, 0, 1)
        qc = client.get("/dashboard/board?view=qc&mine=1").text
        assert 'data-id="7"' in qc and "Ready for testing" in qc
        # Dev still SEES it (it is what Dev handed over) but is not asked to act.
        dev = client.get("/dashboard/board?view=dev").text
        assert 'data-id="7"' in dev and "With QC" in dev
        assert 'data-id="7"' not in client.get("/dashboard/board?view=dev&mine=1").text

        # While the agent is actually running, the column wins over any leftover tag:
        # a working item is nobody's queue.
        state["tags"] = ["vm-autopilot", "autopilot-review", "autopilot-live"]
        state["state"] = "Active"
        running = client.get("/dashboard/board?view=dev").text
        assert 'data-id="7"' in running


def test_editor_reports_waiting_counts_and_shared_turns(tmp_path):
    class _FakeAdo:
        async def get_all_tagged_work_items(self):
            return [_item(1, ["vm-autopilot"]), _item(2, ["vm-autopilot", "autopilot-review"])]

    with _client(tmp_path, trigger_tag="vm-autopilot") as client:
        client.app.state.container.ado = _FakeAdo()
        editor = client.get("/dashboard/board-views").text
        assert "1 waiting · sees 2/2" in editor        # BA holds the queued one
        assert 'data-tag="vm-autopilot"' in editor     # tags really on the board
        # Every process has a real queue out of the box — QC's is the review tag,
        # which needs no ADO change — so neither diagnostic fires. An escalation
        # shared by BA and Dev is NOT flagged: work that stopped legitimately needs
        # more than one role.
        assert "no stage of its own" not in editor
        assert "claim the same hand-off" not in editor


def test_cards_are_only_draggable_where_a_drop_rule_exists(tmp_path):
    """A card that looks draggable and silently snaps back is a lie about the config."""

    class _FakeAdo:
        async def get_all_tagged_work_items(self):
            return [_item(1, ["autopilot"])]

    with _client(tmp_path) as client:                    # board_drop_map empty
        client.app.state.container.ado = _FakeAdo()
        page = client.get("/dashboard/board").text
        assert 'draggable="false"' in page
        assert "drag &amp; drop off" in page

    with _client(tmp_path, board_drop_map=["Done => autopilot-done"]) as client:
        client.app.state.container.ado = _FakeAdo()
        page = client.get("/dashboard/board").text
        assert 'draggable="true"' in page
        assert "drag &amp; drop off" not in page
        # Only the lane whose target column has a rule accepts a drop.
        assert page.count('data-column="Done"') == 1
        assert 'data-column="Queued"' not in page


def test_only_my_turn_never_blanks_a_board(tmp_path):
    """A filter that can only ever remove work must not be able to remove all of it."""

    class _FakeAdo:
        async def get_all_tagged_work_items(self):
            return [_item(1, ["autopilot"])]

    with _client(tmp_path) as client:
        client.app.state.container.ado = _FakeAdo()
        # The raw pipeline claims no turn, so the filter is ignored rather than
        # rendering an empty page that reads as "nothing to do".
        page = client.get("/dashboard/board?mine=1").text
        assert 'data-lane="Queued"' in page and 'data-id="1"' in page
        # A process that does claim lanes still filters.
        ba = client.get("/dashboard/board?view=ba&mine=1").text
        assert 'data-lane="Intake"' in ba and 'data-lane="Delivered"' not in ba


def test_only_ambiguous_handoffs_are_flagged(tmp_path):
    """A warning that fires on the default config is a warning nobody reads."""

    class _FakeAdo:
        async def get_all_tagged_work_items(self):
            return []

    # Fully configured relay: BA and Dev share only the escalation columns → silent.
    with _client(tmp_path, board_review_state="Ready for Review",
                 board_deploy_state="Ready for Deploy") as client:
        client.app.state.container.ado = _FakeAdo()
        page = client.get("/dashboard/board-views").text
        assert "claim the same hand-off" not in page
        assert "no stage of its own" not in page

    # Two processes both claiming a FLOW column is the real ambiguity → flagged.
    stage = [{"name": "s", "columns": ["In progress"], "mine": True}]
    clash = [
        {"key": "a", "label": "A", "stages": stage},
        {"key": "b", "label": "B", "stages": stage},
    ]
    with _client(tmp_path, board_lenses=clash) as client:
        client.app.state.container.ado = _FakeAdo()
        page = client.get("/dashboard/board-views").text
        assert "claim the same hand-off" in page and "In progress" in page


def test_run_button_is_the_hand_off_trigger(tmp_path):
    """The relay stops between roles; Run is the human's "I've read it, go" signal."""
    calls: dict[str, list] = {"tags": [], "removed": [], "states": []}

    class _FakeAdo:
        async def get_all_tagged_work_items(self):
            return [_item(1, ["vm-autopilot", "sdlc:ba"], state="Ready for Dev")]

        async def get_work_item(self, item_id):
            return _item(item_id, ["vm-autopilot", "sdlc:ba"], state="Ready for Dev")

        async def add_tag(self, item_id, tag):
            calls["tags"].append((item_id, tag))

        async def remove_tag(self, item_id, tag):
            calls["removed"].append((item_id, tag))

        async def update_state(self, item_id, state):
            calls["states"].append((item_id, state))

    # The relay's parking spot: BA's hand-off state, which the poller ignores and
    # Dev's board claims as its turn. This is the whole reviewed-hand-off shape.
    relay = [
        {"key": "ba", "label": "BA", "profile": "ba",
         "stages": [{"name": "Intake", "columns": ["Queued"], "mine": True},
                    {"name": "Handed to Dev", "states": ["Ready for Dev"]}]},
        {"key": "dev", "label": "Dev", "profile": "dev",
         "stages": [{"name": "Waiting for me", "states": ["Ready for Dev"], "mine": True},
                    {"name": "Building", "columns": ["In progress"], "mine": True},
                    {"name": "Backlog", "columns": ["Queued"]}]},
    ]
    with _client(tmp_path, trigger_tag="vm-autopilot", sdlc_loop_enabled=True,
                 trigger_states=["Active"], board_lenses=relay) as client:
        client.app.state.container.ado = _FakeAdo()

        # An item parked outside the trigger states shows a Run button on the board
        # of the role whose turn it is — and only there.
        dev = client.get("/dashboard/board?view=dev").text
        assert 'data-run="1"' in dev
        assert 'data-lane="Waiting for me"' in dev      # claimed by STATE, not column
        assert 'data-run="1"' not in client.get("/dashboard/board").text   # not the pipeline
        # The same item is only context on BA's board — its turn is over.
        ba = client.get("/dashboard/board?view=ba").text
        assert 'data-lane="Handed to Dev"' in ba and 'data-run="1"' not in ba

        client.post("/dashboard/board/run", data={"item_id": "1", "view": "dev"})
        # The previous role's profile tag is replaced, not stacked...
        assert (1, "sdlc:dev") in calls["tags"]
        assert (1, "sdlc:ba") in calls["removed"]
        # ...and the item is handed back to the poller: trigger tag + trigger state.
        assert (1, "Active") in calls["states"]


def test_run_is_refused_where_it_would_be_a_lie(tmp_path):
    class _FakeAdo:
        async def get_all_tagged_work_items(self):
            return [_item(1, ["autopilot"])]

        async def get_work_item(self, item_id):
            return _item(item_id, ["autopilot"])

        async def add_tag(self, item_id, tag):
            raise AssertionError("dry run must not write")

        async def update_state(self, item_id, state):
            raise AssertionError("dry run must not write")

    with _client(tmp_path, dry_run=True) as client:
        client.app.state.container.ado = _FakeAdo()
        assert "data-run=" not in client.get("/dashboard/board?view=dev").text
        assert client.post("/dashboard/board/run",
                           data={"item_id": "1", "view": "dev"}).status_code == 204
        # The raw pipeline is a view, not a role — nothing to trigger from it.
        assert client.post("/dashboard/board/run",
                           data={"item_id": "1", "view": "pipeline"}).status_code == 204


def test_a_states_only_lane_survives_a_save(tmp_path):
    """The editor must round-trip a hand-off lane — losing it would silently return
    the item to the previous role's board."""
    form = _Form({
        "lens0_label": "Dev", "lens0_key": "dev", "lens0_profile": "dev",
        "lens0_stage0_name": "Waiting for me",
        "lens0_stage0_states": "Ready for Dev, Ready for Development",
        "lens0_stage0_mine": "on",
        "lens0_stage1_name": "Building", "lens0_stage1_columns": ["In progress"],
    })
    parsed = parse_lens_form(form, COLS)
    first = parsed[0]["stages"][0]
    assert first["states"] == ["Ready for Dev", "Ready for Development"]
    assert first["columns"] == [] and first["drop"] == "" and first["mine"] is True
    assert parsed[0]["profile"] == "dev"
    assert validate_lenses(parsed, COLS) == []

    # A stage with neither a column nor a state is a blank row — dropped, as before.
    empty = _Form({"lens0_label": "X", "lens0_stage0_name": "nothing"})
    assert parse_lens_form(empty, COLS) == []


def test_state_claim_beats_the_column_it_falls_back_to():
    """A parked item reads as 'queued' to the engine; the lane that names its state
    is the one telling the truth about whose turn it is."""
    cfg = Settings(board_lenses=[{
        "key": "dev", "label": "Dev",
        "stages": [
            {"name": "Backlog", "columns": ["Queued"]},
            {"name": "Waiting for me", "states": ["Ready for Dev"], "mine": True},
        ],
    }])
    parked = BoardCard(1, "t", "Ready for Dev", "Queued")
    plain = BoardCard(2, "t", "New", "Queued")
    view = board_view(cfg, "dev")
    grouped = group_lanes({"Queued": [parked, plain]}, view)
    assert [c.id for c in grouped["Waiting for me"]] == [1]
    assert [c.id for c in grouped["Backlog"]] == [2]


def test_a_parked_state_never_falls_back_onto_someone_elses_board(tmp_path):
    """An item waiting for QC is `Queued` to the engine. Without this rule BA's
    intake lane (which covers Queued) would claim it and show a Run button."""
    relay = [
        {"key": "ba", "label": "BA", "profile": "ba", "stages": [
            {"name": "Intake", "columns": ["Queued"], "mine": True},
            {"name": "Handed to Dev", "states": ["Ready for Dev"]}]},
        {"key": "dev", "label": "Dev", "profile": "dev", "stages": [
            {"name": "Waiting for me", "states": ["Ready for Dev"], "mine": True},
            {"name": "Handed to QC", "states": ["Ready for QC"]}]},
        {"key": "qc", "label": "QC", "profile": "qc", "stages": [
            {"name": "Waiting for me", "states": ["Ready for QC"], "mine": True}]},
    ]

    class _FakeAdo:
        async def get_all_tagged_work_items(self):
            return [
                _item(1, ["vm-autopilot"], state="New"),
                _item(2, ["vm-autopilot"], state="Ready for Dev"),
                _item(3, ["vm-autopilot"], state="Ready for QC"),
            ]

    import re

    with _client(tmp_path, trigger_tag="vm-autopilot", trigger_states=["New", "Active"],
                 board_lenses=relay) as client:
        client.app.state.container.ado = _FakeAdo()
        seen, runs = {}, {}
        for key in ("ba", "dev", "qc"):
            page = client.get(f"/dashboard/board?view={key}").text
            seen[key] = sorted(set(re.findall(r'data-id="(\d+)"', page)))
            runs[key] = re.findall(r'data-run="(\d+)"', page)
        # Each item waits on exactly one role, and Run sits only there.
        assert runs == {"ba": ["1"], "dev": ["2"], "qc": ["3"]}
        # A role sees the item it handed on (context) but not one parked two steps away.
        assert seen == {"ba": ["1", "2"], "dev": ["2", "3"], "qc": ["3"]}


def test_tag_brake_relay_run_removes_the_tag_that_stopped_it(tmp_path):
    """The no-ADO-change relay: the machine's own done tag parks the item, the next
    role's board claims it, and Run takes the tag off and starts that role's stages."""
    removed: list[tuple[int, str]] = []
    added: list[tuple[int, str]] = []
    states: list[tuple[int, str]] = []
    parked = _item(7, ["vm-autopilot", "autopilot-done", "sdlc:ba"], state="Active")

    class _FakeAdo:
        async def get_all_tagged_work_items(self):
            return [parked]

        async def get_work_item(self, item_id):
            return parked

        async def add_tag(self, item_id, tag):
            added.append((item_id, tag))

        async def remove_tag(self, item_id, tag):
            removed.append((item_id, tag))

        async def update_state(self, item_id, state):
            states.append((item_id, state))

    relay = [
        {"key": "ba", "label": "BA", "profile": "ba",
         "stages": [{"name": "Intake", "columns": ["Queued"], "mine": True}]},
        {"key": "dev", "label": "Dev", "profile": "dev", "stages": [
            {"name": "Waiting for my review", "tags": ["autopilot-done"], "mine": True},
            {"name": "Coding", "columns": ["In progress"], "mine": True}]},
    ]
    with _client(tmp_path, trigger_tag="vm-autopilot", trigger_states=["Active"],
                 sdlc_loop_enabled=True, board_lenses=relay) as client:
        client.app.state.container.ado = _FakeAdo()

        dev = client.get("/dashboard/board?view=dev").text
        assert 'data-lane="Waiting for my review"' in dev and 'data-run="7"' in dev
        # BA's intake covers Queued, but the tag is claimed by Dev — so BA does not
        # get the item back the moment its own stage finished.
        assert 'data-id="7"' not in client.get("/dashboard/board?view=ba").text

        client.post("/dashboard/board/run", data={"item_id": "7", "view": "dev"})
        assert (7, "autopilot-done") in removed      # the brake comes off
        assert (7, "sdlc:ba") in removed             # the finished role's profile too
        assert (7, "sdlc:dev") in added              # ...replaced by this one
        # No state change: the item never left a trigger state. That is the point of
        # the tag brake — the ADO workflow is untouched, the tag alone holds the item.
        assert states == []


def test_settings_save_does_not_wipe_the_processes(tmp_path, monkeypatch):
    """The processes live on their own page, so a Settings save must merge, not
    replace — losing them would silently return everyone to the raw pipeline."""
    import yaml

    cfg_file = tmp_path / "config.yaml"
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(cfg_file))
    lens = [{"key": "ops", "label": "Ops",
             "stages": [{"name": "Mine", "columns": ["Queued"], "mine": True}]}]
    with _client(tmp_path, board_lenses=lens) as client:
        client.post("/dashboard/board-views", data={
            "lens0_label": "Ops", "lens0_key": "ops",
            "lens0_stage0_name": "Mine", "lens0_stage0_columns": ["Queued"],
            "lens0_stage0_mine": "on",
        }, follow_redirects=False)
        client.post("/dashboard/settings", data={"trigger_tag": "changed"},
                    follow_redirects=False)
        saved = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        assert saved["trigger_tag"] == "changed"
        assert [x["key"] for x in saved["board_lenses"]] == ["ops"]
        assert [v.key for v in board_views(client.app.state.container.config)] == [
            "pipeline", "ops",
        ]


def test_doctor_reads_the_board_the_way_the_board_does():
    from ai_autopilot.doctor import check_board_processes

    # A default install is healthy: the shipped processes name the optional hand-off
    # columns, and not having those states configured is not a misconfiguration.
    levels = {f.level for f in check_board_processes(Settings())}
    assert levels == {"ok"}

    # A process that never becomes anyone's turn is worth saying out loud.
    watch_only = Settings(board_lenses=[
        {"key": "ops", "label": "Ops", "stages": [{"name": "All", "columns": ["Queued"]}]},
    ])
    found = check_board_processes(watch_only)
    assert any("never becomes anyone's turn" in f.title for f in found)

    # ...as is a hand-off tag that no board claims: the item stops where nobody looks.
    orphan = Settings(
        sdlc_profile_tags={"ba": "handoff-dev"},
        board_lenses=[{"key": "dev", "label": "Dev",
                       "stages": [{"name": "Mine", "columns": ["Queued"], "mine": True}]}],
    )
    assert any("not on any board" in f.title for f in check_board_processes(orphan))


def test_handoff_tag_is_added_when_a_profile_completes():
    from ai_autopilot.execution.sdlc_plan import handoff_tag

    cfg = Settings(sdlc_profile_tags={"ba": "handoff-dev", "dev": ""})
    assert handoff_tag("ba", cfg) == "handoff-dev"
    assert handoff_tag("dev", cfg) == ""      # blank = no tag hand-off
    assert handoff_tag("qc", cfg) == ""       # absent = no tag hand-off


def test_state_picker_offers_every_type_not_just_what_is_on_the_board(tmp_path):
    """Requirement states exist whether or not a requirement is in flight. A picker
    built from live items alone offers Task/Bug states only — and the hand-off a BA
    needs most is then the one that cannot be picked."""

    class _FakeAdo:
        async def get_all_tagged_work_items(self):
            return [_item(1, ["autopilot"], state="Active")]

        async def get_states_by_type(self):
            return {
                "Task": ["Active", "Closed"],
                "Requirement": ["New", "Ready for Dev", "Ready for UAT"],
            }

    import re

    with _client(tmp_path) as client:
        client.app.state.container.ado = _FakeAdo()
        page = client.get("/dashboard/board-views").text
        chips = re.findall(r'name="lens0_stage0_states" value="([^"]+)"', page)
        assert {"Ready for Dev", "Ready for UAT", "New"} <= set(chips)   # Requirement's
        assert "Active" in chips                                         # Task's
        # Tick boxes, so a lane can claim SEVERAL spots, plus free text for one ADO
        # does not list. A single text input with a datalist could not do either.
        assert page.count('type="checkbox" name="lens0_stage0_states"') == len(chips)
        assert 'class="lv-extra" name="lens0_stage0_states"' in page


def test_a_lane_can_claim_several_states_at_once():
    """Ticking three boxes posts the field three times; reading only the first would
    quietly reduce the lane to one claim on every save."""
    form = _Form({
        "lens0_label": "QC", "lens0_key": "qc",
        "lens0_stage0_name": "Waiting for me",
        "lens0_stage0_states": ["Ready for Testing", "In Testing", "Ready for UAT, In UAT"],
        "lens0_stage0_tags": ["autopilot-done", ""],
        "lens0_stage0_mine": "on",
    })
    stage = parse_lens_form(form, COLS)[0]["stages"][0]
    assert stage["states"] == ["Ready for Testing", "In Testing", "Ready for UAT", "In UAT"]
    assert stage["tags"] == ["autopilot-done"]


def test_every_map_field_shows_its_own_example():
    """One placeholder for all of them told the tag map and the type map to type a
    state — the fields hardest to tell apart were the ones giving wrong examples."""
    from ai_autopilot.dashboard.settings_form import FIELDS

    maps = [(f.key, f.placeholder) for f in FIELDS if f.kind == "map"]
    assert maps, "no map fields — this test is watching the wrong thing"
    assert all(ph for _, ph in maps), [k for k, ph in maps if not ph]
    assert len({ph for _, ph in maps}) == len(maps)      # no two share an example
    by_key = dict(maps)
    # The type map is the one left here that is easiest to confuse with a state map.
    assert "=>" in by_key["sdlc_type_profiles"] and "Ready for" not in by_key["sdlc_type_profiles"]


def test_profile_resolution_order_is_stated_where_it_is_chosen():
    """Four knobs pick the profile (item tag, machine, type, default). The field that
    starts that chain has to say the order, or the other three read as alternatives."""
    from ai_autopilot.dashboard.settings_form import FIELDS

    pinned = next(f for f in FIELDS if f.key == "sdlc_profile")
    assert "sdlc:" in pinned.help and "type map" in pinned.help and "default" in pinned.help
    order = [f.key for f in FIELDS if f.section.startswith("Closed-loop SDLC")]
    assert order.index("sdlc_profile") < order.index("sdlc_type_profiles")
    assert order.index("sdlc_type_profiles") < order.index("sdlc_default_profile")


def test_the_hand_offs_are_not_editable_here_any_more():
    """They are a role's way OUT, and a role's way out is the next role's way IN —
    two facts that only mean something side by side, which is why they moved to the
    Roles page. Left here they were also filed under a loop that does not gate them:
    the hand-off applies with sdlc_loop_enabled off."""
    from ai_autopilot.dashboard.settings_form import FIELDS

    keys = {f.key for f in FIELDS}
    assert "sdlc_profile_states" not in keys
    assert "sdlc_profile_tags" not in keys


# ── Reviews page filters ─────────────────────────────────────────────────────


def _pr(pid, *, repo="Backend-Fresh", author="MSDN", target="dxfac/development",
        status="awaiting", draft=False, reviewers=()):
    return {"id": pid, "title": f"pr {pid}", "repo": repo, "author": author, "target": target,
            "source": f"feature/{pid}-x", "work_item": pid, "is_draft": draft,
            "status": status, "reviewers": [{"name": n, "vote": v} for n, v in reviewers]}


def test_review_filters_answer_one_question_each(tmp_path):
    from ai_autopilot import dashboard as dash

    prs = [
        _pr(1, reviewers=[("Phong Pham", 0), ("Lam Huynh", 0)]),
        _pr(2, author="Dat Pham", status="approved", reviewers=[("Phong Pham", 10)]),
        _pr(3, repo="Micro-Frontend", target="main", draft=True, status="draft"),
        _pr(4, status="blocked", reviewers=[("Lam Huynh", -10)]),
    ]
    filt = dash._filter_reviews
    me = ["Phong Pham"]

    assert [p["id"] for p in filt(prs, {"status": "approved"}, me)] == [2]
    assert [p["id"] for p in filt(prs, {"status": "blocked"}, me)] == [4]
    assert [p["id"] for p in filt(prs, {"repo": "Micro-Frontend"}, me)] == [3]
    assert [p["id"] for p in filt(prs, {"author": "Dat Pham"}, me)] == [2]
    assert [p["id"] for p in filt(prs, {"target": "main"}, me)] == [3]
    assert [p["id"] for p in filt(prs, {"q": "3"}, me)] == [3]          # id / title / branch
    # "Waiting on me" = I am a reviewer AND have not voted. #2 is mine but already voted.
    assert [p["id"] for p in filt(prs, {"mine": "1"}, me)] == [1]
    # Draft is a flag, not a status: asking for drafts must not depend on which one.
    assert [p["id"] for p in filt(prs, {"status": "draft"}, me)] == [3]
    # No filter, or one nobody set, leaves the list alone.
    assert len(filt(prs, {}, me)) == 4
    assert len(filt(prs, {"status": "all", "repo": "all"}, me)) == 4


def test_the_editor_renders_every_field_the_form_can_submit(tmp_path, monkeypatch):
    """The page rebuilds each lens through a whitelist, so a field left out of it is
    not merely hidden — the form re-posts without it and the next Save DELETES it.

    `mine` and `profile` were missing: "your turn" always drew empty and the Run
    select always read "off", so pressing Save on that page silently wiped both. The
    Board kept showing YOUR TURN (it reads the config, not the form), which is what
    made it look like a display quirk instead of data loss.
    """
    cfg_file = tmp_path / "config.yaml"
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(cfg_file))
    saved = [{
        "key": "ba", "label": "BA", "profile": "ba", "tags": [],
        "stages": [
            {"name": "Intake", "columns": ["Queued"], "tone": "slate", "drop": "Queued",
             "mine": True, "tags": ["handoff-ba"], "states": ["Ready for Analysis"]},
            {"name": "Delivered", "columns": ["Done"], "tone": "green", "drop": "Done"},
        ],
    }]
    with _client(tmp_path, board_lenses=saved) as client:
        page = client.get("/dashboard/board-views").text
        assert 'name="lens0_stage0_mine" checked' in page, "'your turn' lost on the way out"
        assert 'name="lens0_stage1_mine" >' in page or 'name="lens0_stage1_mine">' in page
        assert 'value="ba" selected' in page, "the Run profile was lost on the way out"
        # A lane's parking tags and states must come back selected too, or saving
        # drops them the same way.
        assert 'value="handoff-ba"' in page and "checked" in page
        assert "Ready for Analysis" in page


def test_review_belongs_to_dev_once_qc_has_a_queue_of_its_own():
    """"Ready for review" means waiting on a REVIEWER, and a reviewer is a developer.

    QC held that column only because, before the testing column existed, it was all
    QC would ever have — a workaround that outlived its reason. Dev owns it now, but
    only where QC has a queue of its own: without a testing state that column is
    still QC's only one, and a role with no turn is a board that can only be watched.
    Whichever way it falls, exactly one role may claim it.
    """
    from ai_autopilot.board import board_columns
    from ai_autopilot.lenses import (
        SHARED_COLUMNS, coverage_gaps, default_lenses, my_turn_claims, my_turn_columns,
        view_of,
    )

    shapes = {
        "bare": Settings(),
        "review only": Settings(board_review_state=["Ready for Review"]),
        "review+deploy": Settings(board_review_state=["Ready for Review"],
                                  board_deploy_state=["Ready for Deploy"]),
        "all three": Settings(board_review_state=["Ready for Review"],
                              board_deploy_state=["Ready for Deploy"],
                              board_testing_state=["Ready for Testing"]),
    }
    for label, cfg in shapes.items():
        cols = board_columns(cfg)
        owners: dict[str, list[str]] = {}
        turns = {}
        for lens in default_lenses(cfg):
            view = view_of(lens, cols)
            # No column may be left without a lane, or its cards vanish from that board.
            assert coverage_gaps(lens, cols) == [], f"{label}/{lens['key']} drops a column"
            turns[lens["key"]] = my_turn_columns(view)
            for claim in my_turn_claims(view):
                if claim not in SHARED_COLUMNS:
                    owners.setdefault(claim, []).append(lens["key"])
        clashes = {c: who for c, who in owners.items() if len(who) > 1}
        assert not clashes, f"{label}: two roles claim {clashes}"

    # With a testing queue, review is Dev's and QC waits on the test environment.
    assert "Ready for review" in turns["dev"] and "Ready for review" not in turns["qc"]
    assert turns["qc"] == {"Ready for testing"}

    # Without one, it falls back to QC rather than leaving QC nothing to do.
    cols = board_columns(shapes["review only"])
    by_key = {x["key"]: view_of(x, cols) for x in default_lenses(shapes["review only"])}
    assert my_turn_columns(by_key["qc"]) == {"Ready for review"}
    assert "Ready for review" not in my_turn_columns(by_key["dev"])


def test_run_releases_a_live_tag_no_session_is_behind(tmp_path):
    """Pressing Run is a person saying "this is not moving, go", so it must release
    what is holding the item.

    A run killed with its process leaves the live tag behind and never writes a
    result; the poller skips that tag, so the card sits there while Run reports
    started=1 and nothing happens (#8626). But only the poller knows whether a
    session is REALLY running — clearing the tag under a live console would dispatch
    a second one onto the same branch.
    """
    from types import SimpleNamespace

    removed: list[tuple[int, str]] = []

    class _Ado:
        async def get_all_tagged_work_items(self):
            return []
        async def get_work_item(self, iid):
            return SimpleNamespace(id=iid, title="t", work_item_type="Bug", state="Active",
                                   tags=["vm-autopilot", "autopilot-live"])
        async def remove_tag(self, iid, tag):
            removed.append((iid, tag))
        async def add_tag(self, iid, tag):
            pass
        async def update_state(self, iid, state):
            return True

    with _client(tmp_path, live_tag="autopilot-live", trigger_tag="vm-autopilot") as client:
        client.app.state.container.ado = _Ado()

        # No session tracked → the tag is stranded, and Run clears it.
        client.app.state.poller = SimpleNamespace(has_live_session=lambda _i: False)
        client.post("/dashboard/board/run", data={"item_id": "8626", "view": "ba"})
        assert removed == [(8626, "autopilot-live")]

        # A session IS running → leave it alone, or a second console joins the branch.
        removed.clear()
        client.app.state.poller = SimpleNamespace(has_live_session=lambda _i: True)
        client.post("/dashboard/board/run", data={"item_id": "8626", "view": "ba"})
        assert removed == []
