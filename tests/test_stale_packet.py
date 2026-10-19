import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memory_client


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _summary_with(snapshot):
    memory_client.get_projects = lambda: []
    memory_client.get_tasks = lambda **_: []
    memory_client.get_events = lambda **_: []
    memory_client.get_reconciliation_cadence = lambda: {}
    memory_client._task_table_empty = lambda: False
    memory_client.latest_watson_dashboard_snapshot = lambda: snapshot
    return memory_client.get_summary()


def _packet(generated, expires):
    return {
        "agent_id": "watson",
        "dashboard_version": f"watson:{_iso(generated)}",
        "generated_at": _iso(generated),
        "expires_at": _iso(expires),
        "blocks": [{"type": "text", "body": "PlantSB26 cleared the wall."}],
    }


def test_expired_packet_narrative_is_not_served():
    generated = datetime(2026, 9, 9, 11, 8, 27, tzinfo=timezone.utc)
    result = _summary_with(_packet(generated, generated + timedelta(days=2)))
    assert result["headline"] is None
    assert result["summary"] is None
    assert result["today_packet"]["generated_at"] == "2026-09-09T11:08:27Z"
    assert result["today_packet"]["fresh"] is False


def test_fresh_packet_narrative_is_served():
    generated = datetime.now(timezone.utc) - timedelta(hours=2)
    result = _summary_with(_packet(generated, generated + timedelta(days=2)))
    assert result["headline"]["subline"] == "PlantSB26 cleared the wall."
    assert result["today_packet"]["fresh"] is True


if __name__ == "__main__":
    test_expired_packet_narrative_is_not_served()
    test_fresh_packet_narrative_is_served()
    print("stale packet tests passed")
