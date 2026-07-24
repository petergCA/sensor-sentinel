"""Unit tests for the ExclusionEngine — pure Python, no hass required."""

from custom_components.sensor_sentinel.exclusions import ExclusionEngine


def _engine(options: dict | None = None) -> ExclusionEngine:
    return ExclusionEngine(options or {}, lambda _eid: None)


def test_active_snooze_excludes_until_deadline():
    engine = _engine()
    engine.snooze("sensor.a", until_ts=1000.0)
    assert engine.is_excluded("sensor.a", now_ts=999.0)
    # At/after the deadline the snooze no longer matches, even before pruning.
    assert not engine.is_excluded("sensor.a", now_ts=1000.0)
    assert not engine.is_excluded("sensor.a", now_ts=2000.0)


def test_prune_snoozes_returns_expired_and_keeps_active():
    engine = _engine()
    engine.snooze("sensor.expired", until_ts=100.0)
    engine.snooze("sensor.active", until_ts=9000.0)

    expired = engine.prune_snoozes(now_ts=500.0)

    assert expired == ["sensor.expired"]
    assert engine.snapshot_snoozes() == {"sensor.active": 9000.0}
    # A second prune at the same time is a no-op.
    assert engine.prune_snoozes(now_ts=500.0) == []


def test_load_snoozes_drops_expired_and_junk():
    engine = _engine()
    engine.load_snoozes(
        {"sensor.old": 100.0, "sensor.live": 9000.0, "sensor.junk": "nope"},
        now_ts=500.0,
    )
    assert engine.snapshot_snoozes() == {"sensor.live": 9000.0}


def test_unsnooze_clears_the_mute():
    engine = _engine()
    engine.snooze("sensor.a", until_ts=9000.0)
    engine.unsnooze("sensor.a")
    assert not engine.is_excluded("sensor.a", now_ts=100.0)
