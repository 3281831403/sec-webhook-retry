"""Contract tests for engine.hook.retry.

This file is intentionally immutable from the candidate's point of view.
There are exactly 15 tests: ten map to the ten defects in README.md and five
cover the baseline behaviour.  Every test calls the implementation stubs, so
the initial run must be 15 failed, 0 error.
"""

import pytest

from engine.hook.retry import (
    backoff_delay,
    dedup_key,
    is_duplicate,
    mark_delivered,
    next_attempt,
    retry_batch,
    schedule,
    validate_policy,
)


def _policy(**overrides):
    """Build a fully specified, otherwise valid policy for a test."""
    policy = {
        "base_delay": 1.0,
        "cap": 64.0,
        "jitter": 0.0,
        "max_attempts": 8,
        "dedup_fields": ["event_id", "version"],
        "ttl": 30.0,
    }
    policy.update(overrides)
    return policy


def test_backoff_is_exponential_not_fixed_interval():
    """缺陷 1：退避必须指数增长，不能每次都用固定间隔。"""
    delays = [backoff_delay(attempt, 1.0, 64.0, 0.0) for attempt in (1, 2, 3)]
    assert delays == [1.0, 2.0, 4.0]


def test_backoff_stops_at_cap():
    """缺陷 2：退避必须有硬上限，不能无限指数增长。"""
    assert backoff_delay(8, 2.0, 9.0, 0.0) == 9.0
    assert backoff_delay(20, 2.0, 9.0, 1.0) == 9.0


def test_nonzero_jitter_spreads_retry_times():
    """缺陷 3：抖动系数必须生效，让不同客户端错开重试。"""
    assert backoff_delay(3, 1.0, 64.0, 0.0) == 4.0
    assert backoff_delay(3, 1.0, 64.0, 0.25) == 5.0
    assert backoff_delay(3, 1.0, 64.0, 0.5) == 6.0


def test_dedup_key_changes_when_version_changes():
    """缺陷 4：去重键必须包含版本，新版本不能被当成旧版本。"""
    version_one = {"event_id": "order-42", "version": 1, "timestamp": 100.0}
    version_two = {"event_id": "order-42", "version": 2, "timestamp": 100.0}
    assert dedup_key(version_one) != dedup_key(version_two)


def test_dedup_key_ignores_event_timestamp():
    """缺陷 5：去重键不能包含时间戳，否则每次重试都会换键。"""
    first = {"event_id": "order-42", "version": 1, "timestamp": 100.0}
    second = {
        "event_id": "order-42",
        "version": 1,
        "timestamp": 999.0,
        "received_at": 1000.0,
    }
    assert dedup_key(first) == dedup_key(second)


def test_schedule_sorts_by_delivery_time():
    """缺陷 6：计划必须按投递时间排序，不能沿用插入顺序。"""
    policy = _policy(base_delay=2.0, cap=64.0, jitter=0.0)
    events = [
        {"event_id": "late", "version": 1, "attempt": 4},
        {"event_id": "middle", "version": 1, "attempt": 1},
        {"event_id": "early", "version": 1, "attempt": 0},
    ]
    plan = schedule(events, policy, 100.0)
    assert [item["event_id"] for item in plan] == ["early", "middle", "late"]
    assert [item["deliver_at"] for item in plan] == [102.0, 104.0, 132.0]


def test_mark_delivered_prunes_expired_records():
    """缺陷 7：投递记录必须按 TTL 清理，存储不能无限增长。"""
    store = {"ttl": 10.0, "now": 0.0, "records": {}}
    mark_delivered(store, "old-key", 100.0)
    assert store["records"]["old-key"]["expires_at"] == 110.0
    mark_delivered(store, "new-key", 111.0)
    assert set(store["records"]) == {"new-key"}
    assert store["now"] == 111.0


def test_is_duplicate_ignores_expired_records():
    """缺陷 8：重复判定不能把过期记录算作重复。"""
    store = {"ttl": 10.0, "now": 0.0, "records": {}}
    mark_delivered(store, "expiring-key", 100.0)
    assert is_duplicate(store, "expiring-key") is True
    store["now"] = 110.0
    assert is_duplicate(store, "expiring-key") is False
    store["now"] = 111.0
    assert is_duplicate(store, "expiring-key") is False


def test_validate_policy_reports_missing_safeguards():
    """缺陷 9：策略校验必须发现缺少上限、去重键和零抖动。"""
    valid = _policy(jitter=0.25)
    assert validate_policy(valid) == []

    no_cap = _policy(jitter=0.25)
    no_cap.pop("cap")
    assert validate_policy(no_cap) == ["missing_cap"]

    no_dedup = _policy(jitter=0.25)
    no_dedup.pop("dedup_fields")
    assert validate_policy(no_dedup) == ["missing_dedup_fields"]

    zero_jitter = _policy(jitter=0.0)
    assert validate_policy(zero_jitter) == ["zero_jitter"]


def test_retry_batch_keeps_attempt_counts_independent():
    """缺陷 10：批量调度不能把一个事件的尝试次数复用到下一个事件。"""
    policy = _policy(base_delay=1.0, cap=100.0, jitter=0.0, max_attempts=10)
    events = [
        {"event_id": "first-event", "version": 1, "attempt": 4},
        {"event_id": "second-event", "version": 1, "attempt": 0},
    ]
    batch = retry_batch(events, policy, 200.0)
    assert batch["count"] == 2
    by_id = {item["event_id"]: item for item in batch["plan"]}
    assert by_id["first-event"]["attempt"] == 5
    assert by_id["first-event"]["deliver_at"] == 216.0
    assert by_id["second-event"]["attempt"] == 1
    assert by_id["second-event"]["deliver_at"] == 201.0


def test_first_delivery_uses_base_delay():
    """额外要求：首次投递按基线延迟，不提前也不套用后续退避。"""
    policy = _policy(base_delay=1.5, cap=64.0, jitter=0.0)
    event = {"event_id": "fresh-event", "version": 7}
    plan = schedule([event], policy, 10.0)
    assert len(plan) == 1
    assert plan[0]["event_id"] == "fresh-event"
    assert plan[0]["version"] == 7
    assert plan[0]["attempt"] == 1
    assert plan[0]["deliver_at"] == 11.5
    assert plan[0]["key"] == dedup_key(event)


def test_next_attempt_stays_within_cap():
    """额外要求：下一次尝试的时间同样不能超过退避上限。"""
    policy = _policy(base_delay=2.0, cap=10.0, jitter=0.0, max_attempts=50)
    first = next_attempt(
        {"event_id": "retry-cap", "version": 1, "attempts": 9},
        100.0,
        policy,
    )
    assert first["attempts"] == 10
    assert first["deliver_at"] == 110.0
    assert first["exhausted"] is False

    later = next_attempt(
        {"event_id": "retry-cap", "version": 1, "attempts": 30},
        100.0,
        policy,
    )
    assert later["deliver_at"] == 110.0


def test_second_delivery_of_same_key_is_rejected():
    """额外要求：相同事件键的第二次投递必须被拒绝。"""
    store = {"ttl": 100.0, "now": 0.0, "records": {}}
    key = dedup_key({"event_id": "order-9", "version": 1})
    record = mark_delivered(store, key, 20.0)
    assert record["delivered_at"] == 20.0
    with pytest.raises(ValueError):
        mark_delivered(store, key, 21.0)


def test_new_version_is_not_a_duplicate():
    """额外要求：同一事件的不同版本各有独立去重键，不算重复。"""
    store = {"ttl": 100.0, "now": 0.0, "records": {}}
    key_v1 = dedup_key({"event_id": "order-9", "version": 1})
    key_v2 = dedup_key({"event_id": "order-9", "version": 2})
    mark_delivered(store, key_v1, 10.0)
    assert is_duplicate(store, key_v2) is False
    mark_delivered(store, key_v2, 11.0)
    assert set(store["records"]) == {key_v1, key_v2}


def test_separate_batches_match_combined_batch():
    """额外要求：两批独立调度的结果必须等同于合并后单独调度。"""
    policy = _policy(base_delay=1.0, cap=100.0, jitter=0.0, max_attempts=10)
    first_event = {"event_id": "batch-a", "version": 1, "attempt": 2}
    second_event = {"event_id": "batch-b", "version": 1, "attempt": 0}

    combined = retry_batch([first_event, second_event], policy, 300.0)["plan"]
    left = retry_batch([first_event], policy, 300.0)["plan"]
    right = retry_batch([second_event], policy, 300.0)["plan"]
    expected = sorted(
        left + right,
        key=lambda item: (
            item["deliver_at"],
            item["event_id"],
            str(item["version"]),
            item["key"],
        ),
    )
    assert combined == expected
