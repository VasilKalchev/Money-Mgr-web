"""The editor's transaction filters against the synthetic export in mmbak.py."""
import re

import pytest


def uids(client, qs=""):
    r = client.get(f"/api/transactions/uids?{qs}")
    assert r.status_code == 200
    return sorted(r.get_json()["uids"])


# Default view hides soft-deleted (t5) and mirror (t6) rows.
BASE = ["t1", "t2", "t3", "t4"]


@pytest.mark.parametrize("qs, expected", [
    ("", BASE),
    ("account=a1", ["t1", "t2"]),
    ("account=a1&account=a2", BASE),
    ("type=0", ["t3"]),
    ("type=1", ["t1", "t2", "t4"]),
    ("from=2024-02-10", ["t2", "t3", "t4"]),
    ("to=2024-02-10", ["t1", "t2"]),
    ("from=2024-02-10&to=2024-02-11", ["t2", "t3"]),
    ("amount_min=10", ["t1", "t2", "t3"]),
    ("amount_max=10", ["t1", "t4"]),
    ("amount_min=10&amount_max=25.5", ["t1", "t2"]),
    ("amount_min=abc", BASE),  # unparsable bound is ignored, not an error
    ("entered_currency=cur-eur", BASE),
    ("entered_currency=cur-usd", []),
    ("q_note=lunch", ["t1"]),
    ("q_note=!lunch", ["t2", "t3", "t4"]),  # "!" negates; NULL/empty notes match
    ("q_note=%25", ["t4"]),  # a literal "%", not a wildcard
    ("q_note=_", ["t4"]),  # a literal "_", not a single-char wildcard
    ("q_desc=work", ["t1"]),
    ("has_note=yes", ["t1", "t2", "t4"]),
    ("has_note=no", ["t3"]),
    ("has_desc=yes", ["t1", "t3"]),
    ("has_desc=no", ["t2", "t4"]),
    ("show_deleted=show", BASE + ["t5"]),
    ("show_deleted=only", ["t5"]),
    ("show_deleted=bogus", BASE),
    ("show_mirror=show", BASE + ["t6"]),
    ("show_mirror=only", ["t6"]),
    ("show_deleted=show&show_mirror=show", BASE + ["t5", "t6"]),
])
def test_filter(client, qs, expected):
    assert uids(client, qs) == expected


def test_root_category_pulls_in_children(client):
    assert uids(client, "category=c-food") == ["t1", "t2"]


def test_category_only_excludes_children(client):
    assert uids(client, "category_only=c-food") == ["t1"]


def test_child_category_alone(client):
    assert uids(client, "category=c-food-out") == ["t2"]


def test_combined_filters_narrow(client):
    assert uids(client, "account=a1&type=1&amount_min=20") == ["t2"]


def test_sql_injection_is_bound_not_interpolated(client):
    assert uids(client, "account=a1' OR '1'='1") == []
    assert uids(client, "q_note=' OR 1=1 --") == []
    assert uids(client, "from=x' OR '1'='1") == []
    assert len(uids(client)) == 4  # table intact


def test_page_match_count_narrows(client):
    def count(qs):
        html = client.get(f"/editor/transactions?{qs}").get_data(as_text=True)
        m = re.search(r'data-raw="(\d+)">[^<]*</span> matching transactions', html)
        assert m, "match count not found in page"
        return int(m.group(1))

    assert count("") == 4
    assert count("account=a1") == 2
    assert count("show_deleted=show&show_mirror=show") == 6


def test_group_stats(client):
    # ZDATE values are 1s apart, so with a 1s threshold every row of an
    # account chains to the previous one; a switch of account starts a group.
    r = client.get("/api/transactions/group-stats?seconds=1&show_deleted=show&show_mirror=show")
    d = r.get_json()
    assert d["total_rows"] == 6
    # t1,t2 (a1) | t3,t4 (a2) | t5,t6 (a1)
    assert d["groups"] == 3 and d["multi_groups"] == 3
    assert d["avg_size"] == 2 and d["median_gap_sec"] == 1
