import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor import ParseError, filter_before, parse_day, parse_month

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestParseMonth:
    def test_all_disabled_yields_no_candidates(self):
        days, inventory = parse_month(load("month_all_disabled.html"))
        assert days == []
        assert inventory == {"icon_disabled.svg": 3}

    def test_non_disabled_icon_is_candidate(self):
        days, inventory = parse_month(load("month_one_available.html"))
        assert days == [date(2026, 9, 15)]
        assert inventory == {"icon_disabled.svg": 2, "icon_circle.svg": 1}

    def test_unexpected_html_raises(self):
        with pytest.raises(ParseError):
            parse_month("<html><body>mantenimiento programado</body></html>")


class TestParseDay:
    def test_all_zero_yields_no_slots(self):
        assert parse_day(load("day_zero.html")) == []

    def test_positive_count_yields_slot(self):
        assert parse_day(load("day_two.html")) == [("10:30", 2)]

    def test_unexpected_html_raises(self):
        with pytest.raises(ParseError):
            parse_day("<html><body>error interno</body></html>")


class TestFilterBefore:
    def test_discards_on_and_after_limit(self):
        days = [
            date(2026, 12, 1),
            date(2026, 11, 3),   # el mismo día de la cita actual no mejora nada
            date(2026, 11, 2),
            date(2026, 9, 15),
        ]
        assert filter_before(days, date(2026, 11, 3)) == [
            date(2026, 9, 15),
            date(2026, 11, 2),
        ]

    def test_deduplicates(self):
        days = [date(2026, 9, 15), date(2026, 9, 15)]
        assert filter_before(days, date(2026, 11, 3)) == [date(2026, 9, 15)]
