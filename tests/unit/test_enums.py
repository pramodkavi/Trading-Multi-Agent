"""Tests for src.common.models.enums."""

from __future__ import annotations

from src.common.models.enums import JudgeRuling, ScanSession, SignalDirection


class TestSignalDirection:
    def test_string_equality(self) -> None:
        assert SignalDirection.LONG == "LONG"
        assert SignalDirection.SHORT == "SHORT"

    def test_member_count(self) -> None:
        assert len(SignalDirection) == 2


class TestJudgeRuling:
    def test_three_rulings_exist(self) -> None:
        assert {r.value for r in JudgeRuling} == {
            "PUBLISH",
            "PUBLISH_WITH_CAVEAT",
            "SKIP",
        }

    def test_string_serialization(self) -> None:
        assert JudgeRuling.PUBLISH_WITH_CAVEAT.value == "PUBLISH_WITH_CAVEAT"


class TestScanSession:
    def test_covers_spec_scheduler_windows(self) -> None:
        # SPEC §1.7 lists London, NY, Overlap, Daily Wrap; §1.6 adds Asian, Cooldown.
        for name in ("LONDON", "NY", "OVERLAP", "DAILY_WRAP", "ASIAN", "COOLDOWN", "AD_HOC"):
            assert name in ScanSession.__members__
