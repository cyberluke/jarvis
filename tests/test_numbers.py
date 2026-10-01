"""Tests for Czech/European number & duration formatting (jarvis.utils.numbers)."""

from src.jarvis.utils.numbers import (
    format_int,
    format_decimal,
    format_ms,
    format_seconds,
)


class TestFormatInt:
    def test_thousands_separator_space(self):
        assert format_int(1500) == "1 500"
        assert format_int(1000) == "1 000"
        assert format_int(1234567) == "1 234 567"

    def test_small_and_edge(self):
        assert format_int(0) == "0"
        assert format_int(999) == "999"
        assert format_int(-2500) == "-2 500"

    def test_float_and_none(self):
        assert format_int(12.9) == "13"
        assert format_int(None) == "0"


class TestFormatDecimal:
    def test_comma_decimal(self):
        assert format_decimal(12.5, 1) == "12,5"
        assert format_decimal(1500.25) == "1 500,25"

    def test_zero_decimals(self):
        assert format_decimal(1500, 0) == "1 500"
        assert format_decimal(12.9, 0) == "13"

    def test_negative(self):
        assert format_decimal(-1500.5, 1) == "-1 500,5"


class TestFormatDurations:
    def test_ms(self):
        assert format_ms(1500) == "1 500 ms"
        assert format_ms(12.5, 1) == "12,5 ms"

    def test_seconds(self):
        assert format_seconds(1000) == "1 000 s"
        assert format_seconds(None) == "–"