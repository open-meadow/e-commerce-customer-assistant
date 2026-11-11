"""Clock, canonical timestamps and input validation (docs/01 Section C)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db import clock
from app.db.errors import DatabaseDataIntegrityError
from app.db.validation import (
    validate_category,
    validate_identifier,
    validate_issue_type,
    validate_text,
)

UTC = timezone.utc
NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)


class TestClock:
    def test_utc_now_is_aware_utc_whole_seconds(self):
        value = clock.utc_now()
        assert value.tzinfo is not None and value.utcoffset() == timedelta(0)
        assert value.microsecond == 0

    def test_format_and_parse_round_trip(self):
        text = clock.format_timestamp(NOW)
        assert text == "2026-11-07T12:00:00Z"
        assert clock.parse_timestamp(text) == NOW

    def test_format_converts_offsets_to_utc(self):
        ist = timezone(timedelta(hours=5, minutes=30))
        assert clock.format_timestamp(datetime(2026, 11, 7, 17, 30, tzinfo=ist)) == (
            "2026-11-07T12:00:00Z"
        )

    def test_format_rejects_naive_and_wrong_type(self):
        with pytest.raises(ValueError):
            clock.format_timestamp(datetime(2026, 11, 7))
        with pytest.raises(TypeError):
            clock.format_timestamp("2026-11-07T00:00:00Z")

    @pytest.mark.parametrize(
        "bad",
        [
            "2026-11-07T00:00:00",  # no Z
            "2026-11-07T00:00:00+00:00",  # offset
            "2026-11-07T00:00:00.000Z",  # fractional seconds
            "2026-11-07 00:00:00Z",  # space
            "2026-11-07T00:00:00z",  # lowercase z
            "2026-13-07T00:00:00Z",  # month 13
            "2026-02-30T00:00:00Z",  # impossible date
            "2026-11-07T24:00:00Z",  # hour 24
            "",
        ],
    )
    def test_parse_rejects_noncanonical_or_impossible(self, bad):
        with pytest.raises(ValueError):
            clock.parse_timestamp(bad)

    def test_parse_rejects_non_string(self):
        with pytest.raises(TypeError):
            clock.parse_timestamp(20261107)

    def test_persisted_validation_accepts_equal_and_past(self):
        assert clock.validate_persisted_timestamp("2026-11-07T12:00:00Z", "c", NOW) == NOW
        assert clock.validate_persisted_timestamp("2026-11-01T00:00:00Z", "c", NOW)

    def test_persisted_validation_nullable(self):
        assert clock.validate_persisted_timestamp(None, "c", NOW, nullable=True) is None
        with pytest.raises(DatabaseDataIntegrityError):
            clock.validate_persisted_timestamp(None, "c", NOW)

    @pytest.mark.parametrize(
        "bad", ["2026-11-07T12:00:01Z", "garbage", "2026-02-30T00:00:00Z", 5, b"x"]
    )
    def test_persisted_validation_rejects_future_and_malformed(self, bad):
        with pytest.raises(DatabaseDataIntegrityError):
            clock.validate_persisted_timestamp(bad, "c", NOW)


class TestValidateIdentifier:
    def test_accepts_plain_ids(self):
        assert validate_identifier("C101", "x") == "C101"

    @pytest.mark.parametrize("bad", [None, 5, b"C1", ["C1"]])
    def test_wrong_type_is_type_error(self, bad):
        with pytest.raises(TypeError):
            validate_identifier(bad, "x")

    @pytest.mark.parametrize("bad", ["", "   ", "\t\n", "a\x00b", "\ud800"])
    def test_invalid_values_are_value_error(self, bad):
        with pytest.raises(ValueError):
            validate_identifier(bad, "x")


class TestValidateText:
    def test_preserves_exact_text(self):
        text = "  keep spaces\nand newlines \t "
        assert validate_text(text, "x", 1000) == text

    def test_limit_counts_code_points(self):
        assert validate_text("a" * 1000, "x", 1000)
        with pytest.raises(ValueError):
            validate_text("a" * 1001, "x", 1000)
        # An emoji with a skin-tone modifier is 2 code points, not 1.
        assert len("\U0001F44D\U0001F3FD") == 2
        assert validate_text("\U0001F44D\U0001F3FD" * 500, "x", 1000)
        with pytest.raises(ValueError):
            validate_text("\U0001F44D\U0001F3FD" * 501, "x", 1000)

    @pytest.mark.parametrize("bad", [None, 5, b"abc", ["a"]])
    def test_wrong_type_is_type_error(self, bad):
        with pytest.raises(TypeError):
            validate_text(bad, "x", 1000)

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            "\t\n\r\x0b\x0c ",
            " ",  # NBSP: blank by Python strip()
            " 　",
            "a\x00b",  # embedded NUL
            "\x00",
            "a\ud800b",  # lone surrogate
        ],
    )
    def test_invalid_values_are_value_error(self, bad):
        with pytest.raises(ValueError):
            validate_text(bad, "x", 1000)


class TestEnums:
    @pytest.mark.parametrize("value", ["damaged", "wrong_product", "standard"])
    def test_valid_categories(self, value):
        assert validate_category(value) == value

    @pytest.mark.parametrize("bad", ["Damaged", "other", "", " damaged"])
    def test_invalid_category_value(self, bad):
        with pytest.raises(ValueError):
            validate_category(bad)

    def test_category_type(self):
        with pytest.raises(TypeError):
            validate_category(None)

    @pytest.mark.parametrize("value", ["order", "return", "payment", "product", "general"])
    def test_valid_issue_types(self, value):
        assert validate_issue_type(value) == value

    @pytest.mark.parametrize("bad", ["Order", "refund_issue", "", "other"])
    def test_invalid_issue_type_value(self, bad):
        with pytest.raises(ValueError):
            validate_issue_type(bad)

    def test_issue_type_type(self):
        with pytest.raises(TypeError):
            validate_issue_type(3)
