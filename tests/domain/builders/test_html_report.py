from patient360.domain.builders.html_report import FOOTER, SYMPTOM_MESSAGE, VISIT_MESSAGE, build_html


def footer(lang):
    return f"<p><small>{FOOTER[lang]}</small></p>"

AI = {
    "summary": {"en": "Kidney results are abnormal.", "ar": "نتائج الكلى غير طبيعية."},
    "recommendations": [
        {"test_name": "Estimated (eGFR)", "priority": {"en": "urgent", "ar": "عاجل"}},
        {"test_name": "Urea", "priority": {"en": "important", "ar": "مهم"}},
    ],
}
ARABIC = {"Estimated (eGFR)": "معدل الترشيح الكبيبي", "Urea": "اليوريا"}


def test_english_has_the_summary_and_one_bullet_per_recommended_test_name_only():
    page = build_html(AI, ARABIC)["en"]
    assert page == (
        '<div lang="en"><h2>Summary</h2><p>Kidney results are abnormal.</p>'
        "<h2>Recommended follow-up tests</h2><ul><li>Estimated (eGFR)</li><li>Urea</li></ul>"
        f"<p>{VISIT_MESSAGE['en']}</p>{footer('en')}</div>")
    assert "urgent" not in page and "Reason" not in page


def test_arabic_is_right_to_left_and_uses_the_arabic_test_names_from_our_database():
    page = build_html(AI, ARABIC)["ar"]
    assert page == (
        '<div lang="ar" dir="rtl"><h2>الملخص</h2><p>نتائج الكلى غير طبيعية.</p>'
        "<h2>الفحوصات الموصى بها</h2><ul><li>معدل الترشيح الكبيبي</li><li>اليوريا</li></ul>"
        f"<p>{VISIT_MESSAGE['ar']}</p>{footer('ar')}</div>")


def test_the_symptom_check_message_has_the_symptom_and_its_date_escaped_in_both_languages():
    for lang in ("en", "ar"):
        page = build_html(AI, ARABIC, symptom=("headache <script>x</script>", "2026-10-24"))[lang]
        message = SYMPTOM_MESSAGE[lang].format(date="2026-10-24", symptom="headache &lt;script&gt;x&lt;/script&gt;")
        assert page.endswith(f"</ul><p>{message}</p>{footer(lang)}</div>") and "<script>" not in page
        assert VISIT_MESSAGE[lang] not in page


def test_no_message_when_no_test_is_listed():
    nothing = {"summary": {"en": "s", "ar": "ص"}, "recommendations": [
        {"test_name": "Urea", "change_type": {"en": "removed", "ar": "أزيل"}}]}
    for symptom in (None, ("tired", "2026-10-24")):
        page = build_html(nothing, symptom=symptom)
        assert "<p>These tests" not in page["en"] and "Based on" not in page["en"] and "<ul>" not in page["en"]


def test_a_test_without_an_arabic_name_keeps_its_english_name():
    page = build_html(AI, {"Urea": "اليوريا"})["ar"]
    assert "<li>Estimated (eGFR)</li><li>اليوريا</li>" in page
    assert "<li>Estimated (eGFR)</li>" in build_html(AI)["ar"]


def test_a_test_the_symptom_check_removed_is_not_listed():
    ai = {"summary": {"en": "s", "ar": "ص"}, "recommendations": [
        {"test_name": "Urea", "change_type": {"en": "kept", "ar": "أبقي"}},
        {"test_name": "Cystatin C", "change_type": {"en": "removed", "ar": "أزيل"}},
        {"test_name": "HbA1c", "change_type": {"en": "added", "ar": "أضيف"}}]}
    assert "<li>Urea</li><li>HbA1c</li>" in build_html(ai)["en"] and "Cystatin" not in build_html(ai)["en"]


def test_text_from_the_model_is_escaped():
    ai = {"summary": {"en": "<script>alert(1)</script> & more", "ar": "x"},
          "recommendations": [{"test_name": "<b>Urea</b>"}]}
    page = build_html(ai)["en"]
    assert "<script>" not in page and "&lt;script&gt;" in page and "&amp; more" in page
    assert "<b>" not in page and "&lt;b&gt;Urea" in page


def test_no_recommendations_means_no_tests_section_and_nothing_means_an_empty_page():
    assert "<h2>Recommended" not in build_html({"summary": {"en": "s", "ar": "ص"}, "recommendations": []})["en"]
    assert build_html({}) == {"en": f'<div lang="en">{footer("en")}</div>',
                              "ar": f'<div lang="ar" dir="rtl">{footer("ar")}</div>'}      # the footer is always there
