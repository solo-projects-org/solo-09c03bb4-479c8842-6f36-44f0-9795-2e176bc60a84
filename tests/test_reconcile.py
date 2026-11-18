"""Reconciliation tests: an old publish number must be traceable to current data."""

from __future__ import annotations

import threading

import pytest

from app import domain
from app.db import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "ops.db"))
    s.init()
    return s


def ev(store, event_no, revision, unit, fw, code, at):
    domain.add_event_revision(
        store,
        domain.normalise_event_input(event_no, revision, unit, fw, code, at),
    )


def map_(store, fw, code, revision, category):
    domain.add_mapping_revision(store, fw, code, revision, category)


def publish(store, unit, day):
    with store.read() as conn:
        view = domain.day_view(conn, unit, day)
        token, recipe = view.revision_token, view.recipe
    return domain.publish_day(store, unit, day, token, recipe)


def report(store, publish_id):
    return domain.reconcile_publish(store, publish_id)


def test_unchanged_publish_reconciles_cleanly(store):
    map_(store, "FW-1", "A", 1, "A类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    pid = publish(store, "WTG-01", "2026-01-01")

    r = report(store, pid)
    assert r.review_reasons == []
    assert r.items[0].change_kind == "无变化"
    assert r.items[0].target_status == "current"
    assert r.items[0].target_publish_id == pid
    assert r.items[0].target_publish_category == "A类"
    assert r.data_revision.startswith("E1/M1/P1")


def test_event_correction_shows_old_and_current_classification(store):
    map_(store, "FW-1", "A", 1, "A类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    pid = publish(store, "WTG-01", "2026-01-01")

    # Move E1 to the next day; its old day loses its publish, new day unpublished.
    ev(store, "E1", 2, "WTG-01", "FW-1", "A", "2026-01-02T01:00:00Z")

    r = report(store, pid)
    item = r.items[0]
    assert item.old_unit == "WTG-01" and item.old_day == "2026-01-01"
    assert item.old_category == "A类"
    assert item.current_revision == 2
    assert item.current_day == "2026-01-02"
    assert item.change_kind == "事件更正"
    assert item.moved is True
    # Destination day has no publish yet.
    assert item.target_publish_id is None
    assert item.target_status == "unpublished"
    assert r.old_day_empty is True and r.old_day_all_moved is True
    assert any("2026-01-02" in x for x in r.review_reasons)


def test_old_day_with_remaining_events_not_republished_is_flagged(store):
    map_(store, "FW-1", "A", 1, "A类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    ev(store, "E2", 1, "WTG-01", "FW-1", "A", "2026-01-01T02:00:00Z")
    pid = publish(store, "WTG-01", "2026-01-01")

    # Correct only E1 in-place; the old day still has E1+E2, but no current publish.
    ev(store, "E1", 2, "WTG-01", "FW-1", "A", "2026-01-01T05:00:00Z")

    r = report(store, pid)
    assert r.old_day_all_moved is False
    assert r.old_day_empty is False
    assert r.old_day_publish_id is None
    assert any("仍有 2 个事件" in x and "尚未重发布" in x for x in r.review_reasons)
    assert r.items[0].change_kind == "事件更正"


def test_mapping_revision_detected_and_new_category_shown(store):
    map_(store, "FW-1", "A", 1, "旧类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    pid = publish(store, "WTG-01", "2026-01-01")
    map_(store, "FW-1", "A", 2, "新类")
    publish(store, "WTG-01", "2026-01-01")  # republished under the new mapping

    r = report(store, pid)
    item = r.items[0]
    assert item.change_kind == "映射修订"
    assert item.old_category == "旧类"
    assert item.current_category == "新类"
    assert item.target_publish_category == "新类"
    assert item.target_status == "current"
    assert r.review_reasons == []


def test_event_and_mapping_change_combined(store):
    map_(store, "FW-1", "A", 1, "旧类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    pid = publish(store, "WTG-01", "2026-01-01")
    map_(store, "FW-1", "A", 2, "新类")
    ev(store, "E1", 2, "WTG-01", "FW-1", "A", "2026-01-01T03:00:00Z")
    publish(store, "WTG-01", "2026-01-01")

    item = report(store, pid).items[0]
    assert item.change_kind == "两者共同变化"
    assert item.old_category == "旧类" and item.current_category == "新类"
    assert item.target_status == "current"


def test_missing_mapping_on_target_day_flagged(store):
    map_(store, "FW-1", "A", 1, "A类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    pid = publish(store, "WTG-01", "2026-01-01")

    # Correction points at a firmware/code pair with no mapping at all.
    ev(store, "E1", 2, "WTG-01", "FW-9", "Z", "2026-01-02T01:00:00Z")

    item = report(store, pid).items[0]
    assert item.current_category is None
    assert item.target_status == "missing_mapping"
    assert any("缺少映射" in x for x in item.review_reasons)


def test_target_day_unpublished_vs_revoked_pending(store):
    map_(store, "FW-1", "A", 1, "A类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    ev(store, "E2", 1, "WTG-02", "FW-1", "A", "2026-01-02T01:00:00Z")
    pid = publish(store, "WTG-01", "2026-01-01")
    publish(store, "WTG-02", "2026-01-02")  # published, then revoked by E2 correction

    # E1 -> WTG-02 / 01-02 whose publish is revoked; E2 correction moves it too.
    ev(store, "E2", 2, "WTG-02", "FW-1", "A", "2026-01-05T01:00:00Z")
    ev(store, "E1", 2, "WTG-02", "FW-1", "A", "2026-01-02T03:00:00Z")

    item = report(store, pid).items[0]
    assert item.target_status == "revoked_pending"
    assert any("已撤销" in x and "等待重发布" in x for x in item.review_reasons)


def test_all_moved_marks_empty_day_and_no_publish_required(store):
    map_(store, "FW-1", "A", 1, "A类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    ev(store, "E2", 1, "WTG-01", "FW-1", "A", "2026-01-01T02:00:00Z")
    pid = publish(store, "WTG-01", "2026-01-01")

    ev(store, "E1", 2, "WTG-02", "FW-1", "A", "2026-01-03T01:00:00Z")
    ev(store, "E2", 2, "WTG-02", "FW-1", "A", "2026-01-03T02:00:00Z")

    r = report(store, pid)
    assert r.old_day_empty is True
    assert r.old_day_all_moved is True
    assert r.old_day_live_events == []
    assert r.old_day_publish_id is None
    # An empty day is not "zero" and has no publishable file; it must not be
    # reported as a current zero-value file.
    with store.read() as conn:
        assert domain.day_view(conn, "WTG-01", "2026-01-01").publish is None
        assert ("WTG-01", "2026-01-01") not in {
            (d["unit"], d["day"]) for d in domain.available_days(conn)
        }


def test_report_uses_one_snapshot_under_concurrent_writes(store):
    """While a report is being built, a mapping correction may commit; the
    report must be internally consistent (never old category with new map rev)."""
    map_(store, "FW-1", "A", 1, "旧类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    pid = publish(store, "WTG-01", "2026-01-01")

    seen: list[domain.ReconcileReport] = []
    stop = threading.Event()

    def writer():
        rev = 2
        categories = ["新类-1", "新类-2", "新类-3"]
        while not stop.is_set() and rev <= 30:
            map_(store, "FW-1", "A", rev, categories[(rev - 2) % 3])
            rev += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(40):
            seen.append(report(store, pid))
    finally:
        stop.set()
        t.join()

    # Every report contains exactly one mapping revision in both its "current"
    # columns and its target publish columns, and the revision matches the
    # category recorded in the publish item.
    for r in seen:
        item = r.items[0]
        if item.target_status == "current":
            assert item.target_publish_category == item.current_category
        # data revision token is reproducible by re-reading in a snapshot
        with store.snapshot() as conn:
            assert domain.data_revision(conn).startswith("E1/")
    assert len(seen) == 40


def test_snapshot_isolation_prevents_reading_uncommitted_writes(store):
    """The snapshot connection must not see a writer's uncommitted revision."""
    map_(store, "FW-1", "A", 1, "A类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    pid = publish(store, "WTG-01", "2026-01-01")

    with store.snapshot() as conn:
        before = domain.data_revision(conn)
        with store.write() as w:
            w.execute(
                "INSERT INTO mappings (firmware_version, fault_code, revision,"
                " category, created_at) VALUES ('FW-1', 'A', 2, '新类', 'now')"
            )
            # uncommitted here; committed when the write block exits
        after = domain.data_revision(conn)
        assert before == after
        assert domain.build_reconcile_report(conn, pid).items[0].current_category == "A类"

    with store.read() as conn:
        assert domain.data_revision(conn) != before


def test_unknown_publish_id_raises(store):
    with pytest.raises(LookupError):
        report(store, 999)


def test_destination_summary_groups_events(store):
    map_(store, "FW-1", "A", 1, "A类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    ev(store, "E2", 1, "WTG-01", "FW-1", "A", "2026-01-01T02:00:00Z")
    pid = publish(store, "WTG-01", "2026-01-01")

    ev(store, "E1", 2, "WTG-02", "FW-1", "A", "2026-01-04T01:00:00Z")
    ev(store, "E2", 2, "WTG-02", "FW-1", "A", "2026-01-04T02:00:00Z")
    new_pid = publish(store, "WTG-02", "2026-01-04")

    r = report(store, pid)
    assert len(r.destinations) == 1
    dest = r.destinations[0]
    assert (dest.unit, dest.day) == ("WTG-02", "2026-01-04")
    assert dest.event_nos == ["E1", "E2"]
    assert dest.publish_id == new_pid
    assert r.review_reasons == []


def test_report_header_is_the_requested_publish_not_a_destination(store):
    """Regression: the destination lookup must not shadow the requested publish."""
    map_(store, "FW-1", "A", 1, "A类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    old_pid = publish(store, "WTG-01", "2026-01-01")
    ev(store, "E1", 2, "WTG-02", "FW-1", "A", "2026-01-03T01:00:00Z")
    new_pid = publish(store, "WTG-02", "2026-01-03")

    r = report(store, old_pid)
    assert r.publish["id"] == old_pid
    assert r.publish["day"] == "2026-01-01"
    assert r.publish["status"] == "REVOKED"
    assert r.destinations[0].publish_id == new_pid


def test_target_publish_with_older_event_revision_is_flagged(store):
    """A CURRENT target file must be built from the same event revision the
    reconciliation followed; otherwise the operator is told to re-check."""
    map_(store, "FW-1", "A", 1, "A类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    pid = publish(store, "WTG-01", "2026-01-01")

    # Simulate a hand-edited/corrupt target file that claims revision 0.
    with store.write() as conn:
        conn.execute("UPDATE publish_items SET revision = 0 WHERE publish_id = ?", (pid,))

    item = report(store, pid).items[0]
    assert item.target_status == "publish_stale_event"
    assert any("现行修订为 r1" in x for x in item.review_reasons)
