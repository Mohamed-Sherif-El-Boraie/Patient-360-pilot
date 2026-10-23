from patient360.domain.values import parse_numeric, remove_empty_values


def test_parse_numeric_reads_the_leading_number_like_v3():
    assert parse_numeric("13") == 13 and isinstance(parse_numeric("13"), int)
    assert parse_numeric("8.6") == 8.6
    assert parse_numeric("1.08  L") == 1.08
    assert parse_numeric("1,000") == 1000
    assert parse_numeric("<5") is None
    assert parse_numeric("1:64") is None and parse_numeric("5/10") is None      # titre, ratio
    assert parse_numeric("2026-01-10") is None and parse_numeric("5-10") is None  # date, range text
    assert parse_numeric("Positive") is None
    assert parse_numeric("") is None and parse_numeric(None) is None


def test_remove_empty_values_drops_none_empty_and_empty_containers_but_keeps_zero_and_false():
    source = {"a": None, "b": "", "c": [], "d": {"e": None}, "f": 0, "g": False, "h": [1, None, ""]}
    assert remove_empty_values(source) == {"f": 0, "g": False, "h": [1]}
    assert remove_empty_values([None, ""]) is None
