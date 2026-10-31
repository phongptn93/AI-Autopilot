from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from ai_autopilot.ticket_clusters import (
    Cluster,
    Ticket,
    cluster,
    render_candidate_html,
    render_candidate_title,
    rising,
    similarity,
    tokens,
)

NOW = datetime(2026, 10, 31, tzinfo=UTC)


def _t(i: int, title: str, days_ago: int | None = 1, state: str = "New") -> Ticket:
    created = None if days_ago is None else NOW - timedelta(days=days_ago)
    return Ticket(id=i, title=title, state=state, created=created)


def test_tokens_drop_stopwords_short_numeric_and_ids() -> None:
    """Noise words must not glue unrelated tickets together."""
    toks = tokens("Lỗi không xuất được báo cáo #1234 số 2024 an to")
    assert "loi" not in toks and "khong" not in toks
    assert "1234" not in toks and "2024" not in toks
    assert "an" not in toks and "to" not in toks
    assert {"xuat", "bao", "cao"} <= toks


def test_tokens_accent_insensitive() -> None:
    """Support staff type with and without accents; both must match."""
    assert tokens("đăng nhập") == tokens("dang nhap")


def test_tokens_empty_title() -> None:
    """An empty title must not crash."""
    assert tokens("") == frozenset()


def test_similarity_empty_sets_are_zero() -> None:
    """Two empty sets must not read as identical (0/0)."""
    assert similarity(frozenset(), frozenset()) == 0.0
    assert similarity(frozenset({"abc"}), frozenset()) == 0.0


def test_similarity_jaccard_value() -> None:
    """Jaccard is |A∩B|/|A∪B|."""
    assert similarity(frozenset({"aaa", "bbb"}), frozenset({"bbb", "ccc"})) == 1 / 3
    assert similarity(frozenset({"aaa"}), frozenset({"aaa"})) == 1.0


def test_accented_and_plain_titles_cluster() -> None:
    """The same complaint with/without accents is one cluster."""
    cs = cluster([_t(1, "Xuất báo cáo bị timeout"), _t(2, "Xuat bao cao timeout")])
    assert len(cs) == 1 and cs[0].size == 2


def test_unrelated_titles_stay_apart() -> None:
    """Different topics must not merge."""
    cs = cluster([_t(1, "Xuất báo cáo timeout"), _t(2, "Đăng nhập sai mật khẩu")], min_size=1)
    assert len(cs) == 2


def test_threshold_respected() -> None:
    """A stricter threshold splits a loosely similar pair."""
    ts = [_t(1, "xuat bao cao ton kho"), _t(2, "xuat bao cao cong no")]
    assert len(cluster(ts, threshold=0.4)) == 1
    assert cluster(ts, threshold=0.9) == []


def test_min_size_filters_singletons() -> None:
    """Singletons are not a pattern."""
    ts = [_t(1, "xuat bao cao timeout"), _t(2, "dang nhap loi mat khau")]
    assert cluster(ts) == []
    assert len(cluster(ts, min_size=1)) == 2


def test_no_usable_tokens_never_cluster() -> None:
    """Titles that reduce to nothing must not all collapse into one cluster."""
    ts = [_t(1, "Lỗi"), _t(2, "Lỗi"), _t(3, "123")]
    assert cluster(ts) == []


def test_deterministic_under_shuffle() -> None:
    """Result must not depend on input order."""
    ts = [
        _t(i, title, days_ago=i)
        for i, title in enumerate(
            [
                "xuat bao cao timeout",
                "Xuất báo cáo bị timeout",
                "export bao cao timeout lau",
                "dang nhap sai mat khau",
                "Đăng nhập sai mật khẩu",
                "in phieu giao hang bi mat dong",
                "in phieu giao hang mat dong",
            ],
            start=1,
        )
    ]
    base = [(c.key, [t.id for t in c.tickets], c.label) for c in cluster(ts)]
    for seed in range(5):
        shuffled = ts[:]
        random.Random(seed).shuffle(shuffled)
        assert [(c.key, [t.id for t in c.tickets], c.label) for c in cluster(shuffled)] == base


def test_key_is_oldest_ticket_id() -> None:
    """The key stays stable when newer tickets join, so BA links do not break."""
    cs = cluster([_t(5, "xuat bao cao timeout", 1), _t(9, "xuat bao cao timeout", 30)])
    assert cs[0].key == "9"
    assert [t.id for t in cs[0].tickets] == [9, 5]


def test_clusters_sorted_by_size_then_recency() -> None:
    """Biggest first; equal size -> most recently seen first."""
    ts = [
        _t(1, "xuat bao cao timeout", 50),
        _t(2, "xuat bao cao timeout", 40),
        _t(3, "dang nhap sai mat khau", 5),
        _t(4, "dang nhap sai mat khau", 4),
        _t(5, "in phieu mat dong", 3),
        _t(6, "in phieu mat dong", 2),
        _t(7, "in phieu mat dong", 1),
    ]
    assert [c.key for c in cluster(ts)] == ["5", "3", "1"]


def test_label_is_shortest_among_most_shared() -> None:  # t1 and t3 tie on shared tokens; t3 is shorter
    """The label must be a deterministic, representative title."""
    ts = [
        _t(1, "xuat bao cao timeout rat lau khi chay"),
        _t(2, "xuat bao cao timeout"),
        _t(3, "xuat bao cao timeout lau"),
    ]
    assert cluster(ts, threshold=0.4)[0].label == "xuat bao cao timeout lau"


def test_open_count_case_insensitive() -> None:
    """State comparison must ignore case."""
    c = Cluster("1", "x", [_t(1, "a", state="CLOSED"), _t(2, "a", state="Active"), _t(3, "a", state="done")])
    assert c.open_count(["closed", "Done"]) == 1
    assert c.open_count([]) == 3


def test_rising_true_when_mostly_recent() -> None:
    """Majority of dated tickets inside the window means rising."""
    c = Cluster("1", "x", [_t(1, "a", 40), _t(2, "a", 3), _t(3, "a", 2), _t(4, "a", 1)])
    assert rising(c, now=NOW)


def test_rising_false_when_old_or_exactly_half() -> None:
    """Half is not 'more than half'; old clusters are not rising."""
    old = Cluster("1", "x", [_t(i, "a", 60 + i) for i in range(1, 5)])
    half = Cluster("1", "x", [_t(1, "a", 40), _t(2, "a", 30), _t(3, "a", 2), _t(4, "a", 1)])
    assert not rising(old, now=NOW)
    assert not rising(half, now=NOW)


def test_rising_needs_three_dated_tickets() -> None:
    """Two recent tickets, or undated ones, are too thin to call a trend."""
    assert not rising(Cluster("1", "x", [_t(1, "a", 1), _t(2, "a", 1)]), now=NOW)
    undated = Cluster("1", "x", [_t(1, "a", 1), _t(2, "a", 1), _t(3, "a", None), _t(4, "a", None)])
    assert not rising(undated, now=NOW)


def test_naive_and_aware_datetimes_do_not_crash() -> None:
    """Mixed tz-awareness is common across ADO payloads; treat naive as UTC."""
    naive = Ticket(1, "xuat bao cao timeout", created=datetime(2026, 10, 1))
    aware = Ticket(2, "xuat bao cao timeout", created=datetime(2026, 10, 2, tzinfo=UTC))
    cs = cluster([aware, naive])
    assert cs[0].key == "1"
    assert cs[0].first_seen is not None and cs[0].last_seen is not None
    assert rising(cs[0], now=datetime(2026, 10, 31)) is False


def test_undated_tickets_sort_last() -> None:
    """Undated tickets must not become the 'oldest' key."""
    cs = cluster([_t(1, "xuat bao cao timeout", None), _t(2, "xuat bao cao timeout", 5)])
    assert cs[0].key == "2"
    assert [t.id for t in cs[0].tickets] == [2, 1]


def test_html_escapes_titles() -> None:
    """A hostile title must never become live markup in the work item."""
    c = Cluster("1", "<script>x</script>", [_t(1, "<script>alert(1)</script>"), _t(2, "b")])
    html = render_candidate_html(c)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_html_caps_listed_tickets() -> None:
    """Long clusters list 10 tickets and summarise the rest."""
    c = Cluster("1", "x", [_t(i, f"t{i}") for i in range(1, 14)])
    html = render_candidate_html(c, now=NOW)
    assert html.count("<li>") == 10
    assert "… và 3 ticket nữa" in html
    assert "xác nhận" in html


def test_html_no_overflow_line_when_small() -> None:
    """No 'và N ticket nữa' line when everything is listed."""
    c = Cluster("1", "x", [_t(1, "a"), _t(2, "b")])
    assert "ticket nữa" not in render_candidate_html(c)


def test_html_shows_open_count_when_closed_states_given() -> None:
    """Open count appears so the BA sees whether the pain is ongoing."""
    c = Cluster("1", "x", [_t(1, "a", state="Closed"), _t(2, "b", state="New")])
    assert "còn mở: 1" in render_candidate_html(c, closed_states=["Closed"])


def test_candidate_title_prefix_and_truncation() -> None:
    """Titles stay within ADO-friendly length."""
    short = Cluster("1", "xuất báo cáo", [])
    assert render_candidate_title(short) == "[Phản hồi production] xuất báo cáo"
    long = Cluster("1", "a" * 300, [])
    assert len(render_candidate_title(long)) == 120
