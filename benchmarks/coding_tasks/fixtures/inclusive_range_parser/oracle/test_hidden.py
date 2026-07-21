import pytest

from ranges import parse_range


def test_parse_range_accepts_surrounding_whitespace():
    assert parse_range(" 3 - 5 ") == [3, 4, 5]


def test_parse_range_rejects_descending_values():
    with pytest.raises(ValueError, match="descending"):
        parse_range("5-3")
