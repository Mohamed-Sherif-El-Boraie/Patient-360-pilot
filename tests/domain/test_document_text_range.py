from patient360.domain.document import build_visit_document


def doc_row(range_text):
    raw = {"source": "urgent", "patient": {"age_years": 30, "sex": "male"}, "tests": [{"test_name": "T", "results": [
        {"parameter_name": "P", "value": "5", "reference": {"range": range_text}}]}]}
    return build_visit_document(raw).document["tests"][0]["results"][0]


def test_a_not_set_text_range_does_not_become_bounds():
    for marker in ("-1 - 9999", "0 - 0"):
        row = doc_row(marker)
        assert "low" not in row.get("reference", {}) and "high" not in row.get("reference", {})
        assert "status" not in row


def test_a_real_text_range_still_classifies():
    assert doc_row("1 - 4")["status"] == "high"
