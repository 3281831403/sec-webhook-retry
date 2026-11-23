"""Webhook retry contracts.

This module is the only implementation file for the exercise.  Every public
function below is a stub: implement the documented contract and keep all
numeric assertions exact (tolerance 0).  No random source and no system clock
may be used; callers pass now and the jitter coefficient explicitly.
"""

from __future__ import annotations

import hashlib
import json


def backoff_delay(attempt: int, base: float, cap: float, jitter: float) -> float:
    """Return the delay in seconds before the 1-based retry ``attempt``.

    The curve is::

        raw = base * (2 ** (attempt - 1))
        delay = min(cap, raw * (1 + jitter))

    ``base`` is the first-retry baseline, ``cap`` is a hard upper bound and
    ``jitter`` is a non-negative coefficient supplied by the caller.  Different
    clients can pass different coefficients to spread their retries; no random
    number generator is involved.  The result is always in ``0..cap`` and is
    compared exactly by callers (tolerance 0).

    Raises:
        ValueError: if ``attempt < 1``, ``base <= 0``, ``cap <= 0`` or
            ``jitter < 0``.
    """
    # Defect 1: the curve must be exponential (base * 2 ** (attempt - 1)),
    # never a fixed interval.
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    if base <= 0:
        raise ValueError("base must be positive")
    # Defect 2: ``cap`` is a hard upper bound applied to the jittered value.
    if cap <= 0:
        raise ValueError("cap must be positive")
    # Defect 3: a non-zero coefficient must actually spread the retries.
    if jitter < 0:
        raise ValueError("jitter must be non-negative")

    try:
        raw = base * (2.0 ** (attempt - 1))
        jittered = raw * (1 + jitter)
    except OverflowError:
        # An astronomically large attempt is still bounded by the cap.
        return float(cap)
    return float(min(cap, jittered))


def dedup_key(event: dict) -> str:
    """Return a stable deduplication key for ``event``.

    ``event`` must contain ``event_id`` and ``version``.  Those two values are
    encoded as canonical JSON ``[event_id, version]`` and hashed with SHA-256;
    the lowercase hexadecimal digest is returned.  All volatile fields, such as
    ``timestamp``, ``received_at`` or transport metadata, are ignored, so the
    key is identical across retries of the same event.  A different version of
    the same event must produce a different key.

    Raises:
        KeyError: if ``event_id`` or ``version`` is missing.
    """
    # Defect 4: version is part of the identity, so a new version is distinct.
    # Defect 5: volatile fields such as timestamps are deliberately excluded.
    event_id = event["event_id"]
    version = event["version"]
    canonical = json.dumps([event_id, version], separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def schedule(events: list[dict], policy: dict, now: float) -> list[dict]:
    """Build a time-sorted delivery plan for ``events``.

    Each event has ``event_id`` and ``version`` plus an optional ``attempt``
    field holding the number of completed attempts (default 0).  The delivery
    number for that event is therefore ``attempt + 1``.  ``policy`` provides
    ``base_delay``, ``cap``, ``jitter`` and ``max_attempts``.

    A plan item is a dict with ``key``, ``event_id``, ``version``, ``attempt``
    (the delivery number) and ``deliver_at``, where ``deliver_at`` is
    ``now + backoff_delay(delivery_number, base_delay, cap, jitter)``.  Events
    whose delivery number exceeds ``max_attempts`` are skipped.  The result is
    sorted by ``deliver_at`` ascending, then by ``event_id``, ``version`` and
    ``key`` ascending for ties.
    """
    base_delay = policy["base_delay"]
    cap = policy["cap"]
    jitter = policy["jitter"]
    max_attempts = policy["max_attempts"]

    plan: list[dict] = []
    for event in events:
        delivery_number = event.get("attempt", 0) + 1
        if delivery_number > max_attempts:
            continue
        plan.append(
            {
                "key": dedup_key(event),
                "event_id": event["event_id"],
                "version": event["version"],
                "attempt": delivery_number,
                "deliver_at": now
                + backoff_delay(delivery_number, base_delay, cap, jitter),
            }
        )

    # Defect 6: order by earliest delivery time rather than insertion order.
    plan.sort(
        key=lambda item: (
            item["deliver_at"],
            item["event_id"],
            str(item["version"]),
            item["key"],
        )
    )
    return plan


def mark_delivered(store: dict, key: str, now: float) -> dict:
    """Record a successful delivery for ``key`` and return the stored record.

    ``store`` is a mutable dict with ``ttl`` (record lifetime in seconds),
    ``now`` (the latest explicit time) and ``records`` (key to record mapping).
    A record has the shape ``{key, delivered_at, expires_at}``.

    The call first removes records whose ``expires_at`` is less than or equal
    to ``now``.  If ``key`` still has a live record, the delivery is a
    duplicate and ``ValueError`` is raised.  Otherwise a new record is written,
    ``store["now"]`` is set to ``now``, and the new record is returned.
    """
    ttl = store["ttl"]
    records = store["records"]

    # Defect 7: prune expired records so the store cannot grow without bound.
    expired = [
        existing_key
        for existing_key, record in records.items()
        if record["expires_at"] <= now
    ]
    for existing_key in expired:
        del records[existing_key]

    if key in records:
        raise ValueError(f"duplicate delivery for key: {key}")

    record = {
        "key": key,
        "delivered_at": now,
        "expires_at": now + ttl,
    }
    records[key] = record
    store["now"] = now
    return record


def is_duplicate(store: dict, key: str) -> bool:
    """Return whether ``key`` is still inside the duplicate-suppression window.

    Only records whose ``expires_at`` is strictly greater than ``store["now"]``
    participate.  Expired records do not count as duplicates, so a legitimate
    retry after the TTL is accepted.  A missing key returns ``False``.  The
    function never reads the system clock.
    """
    record = store["records"].get(key)
    if record is None:
        return False
    # Defect 8: an expired record must not block a legitimate retry.
    return record["expires_at"] > store["now"]


def next_attempt(state: dict, now: float, policy: dict) -> dict:
    """Compute the next delivery after a failed attempt.

    ``state`` contains ``event_id``, ``version`` and ``attempts`` (the number
    of attempts already performed).  ``policy`` has the same fields as in
    :func:`schedule`.

    The returned dict contains ``key``, ``event_id``, ``version``, ``attempts``
    (the next delivery number), ``deliver_at`` and ``exhausted``.  When the next
    number does not exceed ``max_attempts``, ``deliver_at`` is
    ``now + backoff_delay(next_number, base_delay, cap, jitter)`` and
    ``exhausted`` is ``False``.  When it exceeds the limit, ``deliver_at`` is
    ``None`` and ``exhausted`` is ``True``.  ``state`` must not be mutated.
    """
    attempts = state["attempts"]
    next_number = attempts + 1
    max_attempts = policy["max_attempts"]

    if next_number > max_attempts:
        return {
            "key": dedup_key(state),
            "event_id": state["event_id"],
            "version": state["version"],
            "attempts": next_number,
            "deliver_at": None,
            "exhausted": True,
        }

    return {
        "key": dedup_key(state),
        "event_id": state["event_id"],
        "version": state["version"],
        "attempts": next_number,
        "deliver_at": now
        + backoff_delay(
            next_number,
            policy["base_delay"],
            policy["cap"],
            policy["jitter"],
        ),
        "exhausted": False,
    }


def validate_policy(policy: dict) -> list[str]:
    """Return a list of policy problem codes; an empty list means valid.

    The codes are emitted in this fixed order:

    ``missing_cap``
        ``cap`` is absent or not positive.
    ``missing_dedup_fields``
        ``dedup_fields`` is absent or empty.
    ``zero_jitter``
        ``jitter`` equals 0.
    ``invalid_base_delay``
        ``base_delay`` is absent or not positive.
    ``invalid_max_attempts``
        ``max_attempts`` is absent or less than 1.

    The function only reports problems and never raises.  Zero jitter is a
    warning: such a policy can still be scheduled.
    """
    problems: list[str] = []

    # Defect 9: the cap must be checked first; without it retries never bound.
    cap = policy.get("cap")
    if cap is None or cap <= 0:
        problems.append("missing_cap")

    dedup_fields = policy.get("dedup_fields")
    if not dedup_fields:
        problems.append("missing_dedup_fields")

    if policy.get("jitter") == 0:
        problems.append("zero_jitter")

    base_delay = policy.get("base_delay")
    if base_delay is None or base_delay <= 0:
        problems.append("invalid_base_delay")

    max_attempts = policy.get("max_attempts")
    if max_attempts is None or max_attempts < 1:
        problems.append("invalid_max_attempts")

    return problems


def retry_batch(events: list[dict], policy: dict, now: float) -> dict:
    """Build a batch delivery plan with independent attempt state per event.

    ``events`` and ``policy`` follow :func:`schedule`.  The result is a dict
    with ``plan`` (the same plan items as :func:`schedule`, sorted the same
    way), ``count`` (number of plan items) and ``now`` (the supplied time).

    Every event's delivery number is derived only from that event's own
    ``attempt`` field.  A previous event's counter must never leak into the
    next event, so the result equals scheduling each event alone and merging
    the plans.
    """
    # Defect 10: each event's delivery number comes from its own ``attempt``
    # field.  Delegating to :func:`schedule` keeps every counter independent.
    plan = schedule(events, policy, now)
    return {"plan": plan, "count": len(plan), "now": now}
