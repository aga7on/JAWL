from ranges import parse_range


def test_parse_range_is_inclusive():
    assert parse_range("2-4") == [2, 3, 4]


def test_parse_single_value_range():
    assert parse_range("7-7") == [7]
