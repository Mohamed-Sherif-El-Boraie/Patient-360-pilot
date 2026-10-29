import pytest

from patient360.domain.payload.values import html_to_plain_text, parse_numeric, remove_empty_values, tidy_number


@pytest.mark.parametrize(("given", "tidy"), [
    ("2 .9", "2.9"), ("- 8.6", "-8.6"), ("57 .0", "57.0"), ("115 .34", "115.34"), ("7.330", "7.330"),
    (" 4 . 5 ", "4.5"), ("+ 3", "+3"), ("12", "12"), ("1 2", "1 2"), ("Positive", "Positive"), ("<5", "<5"),
    ("5 - 10", "5 - 10"), ("", ""), (" ", " "), (None, None), (7, 7),
])
def test_tidy_number_removes_spaces_around_the_sign_and_the_point_only(given, tidy):
    assert tidy_number(given) == tidy


def test_parse_numeric_reads_the_leading_number_like_v3():
    assert parse_numeric("13") == 13 and isinstance(parse_numeric("13"), int)
    assert parse_numeric("8.6") == 8.6
    assert parse_numeric("1.08  L") == 1.08
    assert parse_numeric("1,000") is None and parse_numeric("0,5") is None   # decimal or thousands: we can not tell
    assert parse_numeric("<5") is None
    assert parse_numeric("1:64") is None and parse_numeric("5/10") is None      # titre, ratio
    assert parse_numeric("2026-01-10") is None and parse_numeric("5-10") is None  # date, range text
    assert parse_numeric("Positive") is None
    assert parse_numeric("") is None and parse_numeric(None) is None


def test_remove_empty_values_drops_none_empty_and_empty_containers_but_keeps_zero_and_false():
    source = {"a": None, "b": "", "c": [], "d": {"e": None}, "f": 0, "g": False, "h": [1, None, ""]}
    assert remove_empty_values(source) == {"f": 0, "g": False, "h": [1]}
    assert remove_empty_values([None, ""]) is None


def test_a_blank_text_is_removed_but_text_that_looks_like_a_number_is_kept_exactly():
    source = {"remark": "   ", "tab": "\t\n", "note": "2.5", "high": "--0.24", "low": 0.7, "text": " ok "}
    assert remove_empty_values(source) == {"note": "2.5", "high": "--0.24", "low": 0.7, "text": " ok "}


def test_html_becomes_plain_text_with_line_breaks():
    assert html_to_plain_text("<p>Normal <b>findings</b></p><p>No mass seen</p>") == "Normal findings\nNo mass seen"
    assert html_to_plain_text("line one<br/>line two<BR>line three") == "line one\nline two\nline three"
    assert html_to_plain_text("<ul><li>a</li><li>b</li></ul>") == "a\nb"


def test_entities_are_decoded_and_spaces_collapse():
    assert html_to_plain_text("A &amp; B &lt;5 &nbsp;mg") == "A & B <5 mg"
    assert html_to_plain_text("  too    many \n\n spaces  ") == "too many\nspaces"


def test_script_and_style_content_is_dropped():
    assert html_to_plain_text("<style>p{color:red}</style><p>ok</p><script>alert(1)</script>") == "ok"


def test_a_text_that_only_looks_like_a_tag_is_kept_and_other_types_pass_through():
    assert html_to_plain_text("value a < b and c > d") == "value a < b and c > d"
    assert html_to_plain_text(None) is None and html_to_plain_text(5) == 5 and html_to_plain_text("") == ""
