from patient360.domain.builders.html_report import (
    FOOTER,
    FOOTER_HTML,
    H1,
    H2,
    LABELS,
    NO_TESTS,
    SYMPTOM_MESSAGE,
    TEST,
    WRAPPER,
    build_html,
)


def page(lang, *parts):
    return WRAPPER[lang].format("".join(parts))


def header(lang):
    return H1.format(LABELS[lang]["title"])


def footer(lang):
    return FOOTER_HTML.format(FOOTER[lang])


AI = {
    "summary": {"en": "Kidney results are abnormal.", "ar": "نتائج الكلى غير طبيعية."},
    "simple_summary": {"en": "Your kidney tests need a look.", "ar": "تحاليل الكلى تحتاج متابعة."},
    "recommendations": [
        {"test_name": "Estimated (eGFR)", "priority": {"en": "urgent", "ar": "عاجل"},
         "reason": {"en": "Creatinine is raised.", "ar": "الكرياتينين مرتفع."},
         "timing": {"en": "This week.", "ar": "هذا الأسبوع."}},
        {"test_name": "Urea", "priority": {"en": "important", "ar": "مهم"}},
    ],
}
ARABIC = {"Estimated (eGFR)": "معدل الترشيح الكبيبي", "Urea": "اليوريا"}


def test_english_page_is_header_summary_tests_with_reason_and_one_footer():
    result = build_html(AI, ARABIC)["en"]
    assert result == page(
        "en", header("en"), H2.format("Summary"), '<p style="margin:0">Kidney results are abnormal.</p>',
        H2.format("Recommended follow-up tests"),
        TEST.format('<strong><span dir="ltr">1.</span> Estimated (eGFR)</strong> - Urgent'
                    '<br><u>Reason</u>: Creatinine is raised.'),
        TEST.format('<strong><span dir="ltr">2.</span> Urea</strong> - Important'), footer("en"))
    assert result.count(FOOTER["en"]) == 1
    assert "Plain-language" not in result and "kidney tests need" not in result    # simple_summary is not in the html
    assert "When" not in result and "This week" not in result                      # the timing is not in the html


def test_arabic_is_right_to_left_and_uses_the_arabic_names_and_words():
    result = build_html(AI, ARABIC)["ar"]
    assert result == page(
        "ar", header("ar"), H2.format("الملخص"), '<p style="margin:0">نتائج الكلى غير طبيعية.</p>',
        H2.format("الفحوصات الموصى بها"),
        TEST.format('<strong><span dir="ltr">1.</span> معدل الترشيح الكبيبي</strong> - عاجل'
                    '<br><u>السبب</u>: الكرياتينين مرتفع.'),
        TEST.format('<strong><span dir="ltr">2.</span> اليوريا</strong> - مهم'), footer("ar"))
    assert result.startswith('<div lang="ar" dir="rtl" style="')


def test_the_numbers_are_text_not_list_markers_and_the_page_uses_inline_styles_only():
    for result in build_html(AI, ARABIC).values():
        assert '<span dir="ltr">1.</span>' in result and '<span dir="ltr">2.</span>' in result
        assert "<ol" not in result and "<ul" not in result and "<li" not in result and "<style" not in result


def test_the_numbers_go_on_without_a_gap_when_the_symptom_check_removed_a_test():
    ai = {"recommendations": [{"test_name": "A", "change_type": {"en": "removed"}}, {"test_name": "B"},
                              {"test_name": "C", "change_type": {"en": "removed"}}, {"test_name": "D"}]}
    result = build_html(ai)["en"]
    assert '<span dir="ltr">1.</span> B' in result and '<span dir="ltr">2.</span> D' in result
    assert "3." not in result


def test_the_symptom_check_has_its_line_under_the_tests_heading_in_both_languages():
    for lang in ("en", "ar"):
        result = build_html(AI, ARABIC, symptom=("headache <script>x</script>", "2026-10-24"))[lang]
        line = SYMPTOM_MESSAGE[lang].format(date="2026-10-24", symptom="headache &lt;script&gt;x&lt;/script&gt;")
        assert f"{H2.format(LABELS[lang]['tests'])}<p style=\"margin:0 0 12px\">{line}</p>{TEST[:3]}" in result
        assert "<script>" not in result and result.count(FOOTER[lang]) == 1


def test_no_symptom_line_for_a_visit():
    assert "Based on" not in build_html(AI)["en"]


def test_zero_recommended_tests_show_the_heading_and_the_no_tests_message_in_both_languages():
    removed = {"test_name": "Urea", "change_type": {"en": "removed", "ar": "أزيل"}}
    for ai in ({"summary": {"en": "s", "ar": "ص"}, "recommendations": []},
               {"summary": {"en": "s", "ar": "ص"}, "recommendations": [removed]}):      # none, or all removed
        for lang, page_html in build_html(ai).items():
            assert H2.format(LABELS[lang]["tests"]) + TEST.format(NO_TESTS[lang]) in page_html
            assert "</span>" not in page_html and page_html.count(FOOTER[lang]) == 1


def test_a_symptom_check_with_no_test_left_shows_its_symptom_line_and_the_message():
    ai = {"recommendations": [{"test_name": "Urea", "change_type": {"en": "removed", "ar": "أزيل"}}]}
    result = build_html(ai, symptom=("tired", "2026-10-24"))["en"]
    assert "Based on the symptoms you added on 2026-10-24 (tired)." in result and NO_TESTS["en"] in result


def test_a_test_without_an_arabic_name_keeps_its_english_name():
    result = build_html(AI, {"Urea": "اليوريا"})["ar"]
    assert " Estimated (eGFR)</strong>" in result and " اليوريا</strong>" in result


def test_a_test_the_symptom_check_removed_is_not_listed():
    ai = {"summary": {"en": "s", "ar": "ص"}, "recommendations": [
        {"test_name": "Urea", "change_type": {"en": "kept", "ar": "أبقي"}},
        {"test_name": "Cystatin C", "change_type": {"en": "removed", "ar": "أزيل"}},
        {"test_name": "HbA1c", "change_type": {"en": "added", "ar": "أضيف"}}]}
    result = build_html(ai)["en"]
    assert "1.</span> Urea</strong>" in result and "2.</span> HbA1c</strong>" in result and "Cystatin" not in result


def test_text_from_the_model_is_escaped():
    ai = {"summary": {"en": "<script>alert(1)</script> & more", "ar": "x"},
          "recommendations": [{"test_name": "<b>Urea</b>", "reason": {"en": "<i>r</i>", "ar": "x"}}]}
    result = build_html(ai)["en"]
    assert "<script>" not in result and "&lt;script&gt;" in result and "&amp; more" in result
    assert "<b>Urea" not in result and "&lt;b&gt;Urea" in result and "<i>" not in result


def test_a_blocked_answer_has_no_tests_section_and_nothing_means_header_and_footer_only():
    blocked = {"summary": {"en": "blocked", "ar": "محجوب"}, "recommendations": None}     # see blocked_ai
    assert "Recommended" not in build_html(blocked)["en"] and NO_TESTS["en"] not in build_html(blocked)["en"]
    assert build_html({}) =={lang: page(lang, header(lang), footer(lang)) for lang in ("en", "ar")}
