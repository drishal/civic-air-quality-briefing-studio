"""Verifier tests: citations must exist, numbers must be grounded."""
from __future__ import annotations

from core.models import Finding
from core.verify import check_object, check_text
from llm.schemas import Interpretation, Point


def make_findings() -> list[Finding]:
    return [
        Finding(
            id="F1",
            type="hotspot",
            title="Hotspot: Ward 12 at AQI 180",
            facts={"aqi": 180.0, "pm25": 30.0, "who_multiple": 2.0, "reliability": 0.82},
            formula="worst sub-index of the day",
            evidence_row_ids=["R1", "R2"],
            stations=["Ward 12"],
        ),
        Finding(
            id="F2",
            type="trend",
            title="Ward 12: AQI trend down",
            facts={"slope_per_day": -2.5},
            formula="least-squares slope",
        ),
    ]


def kinds(issues) -> set[str]:
    return {issue.kind for issue in issues}


def test_supported_sentences_pass():
    findings = make_findings()
    text = (
        "PM2.5 averaged 30 µg/m³, about 2 times the WHO limit [F1]. "
        "The station reliability was 0.82 [F1]. "
        "The AQI is falling by 2.5 points a day [F2]."
    )
    assert check_text(text, findings) == []


def test_rounding_tolerance_accepts_close_numbers():
    findings = make_findings()
    assert check_text("The AQI reached 180.4 [F1].", findings) == []
    assert check_text("The AQI reached 179.6 [F1].", findings) == []


def test_unknown_citation_is_caught():
    findings = make_findings()
    issues = check_text("The trend is downward [F9].", findings)
    assert "unknown_citation" in kinds(issues)
    assert any("F9" in issue.detail for issue in issues)


def test_unsupported_number_is_caught():
    findings = make_findings()
    issues = check_text("The AQI reached 412 during the evening [F1].", findings)
    assert "unsupported_number" in kinds(issues)
    assert any("412" in issue.detail for issue in issues)


def test_uncited_number_and_claim_are_caught():
    findings = make_findings()
    issues = check_text("The AQI reached 412 without any citation.", findings)
    assert "uncited_claim" in kinds(issues)

    claim = check_text("PM2.5 is the dominant pollutant.", findings)
    assert "uncited_claim" in kinds(claim)


def test_dates_years_ordinals_and_safe_numbers_are_ignored():
    findings = make_findings()
    text = (
        "On 2026-11-03 the 24-hour PM2.5 average was 30 µg/m³ [F1]. "
        "This was the 2nd worst day, at 19:00 [F1]."
    )
    assert check_text(text, findings) == []


def test_extra_facts_support_numbers():
    findings = make_findings()
    assert check_text("The cleaner used 4200 rows [F1].", findings, {"cleaning": {"n_rows": 4200}}) == []
    assert (
        check_text("The cleaner used 4200 rows [F1].", findings, {"cleaning": {"n_rows": 4200}})
        == []
    )
    issues = check_text("The cleaner used 4200 rows [F1].", findings, {"n_rows": 99})
    assert "unsupported_number" in kinds(issues)


def test_check_object_walks_text_and_cites():
    findings = make_findings()
    interpretation = Interpretation(
        headline="PM2.5 averaged 30 µg/m³ [F1].",
        trend_points=[
            Point(text="The AQI reached 412 yesterday [F1].", cites=["F1"]),
            Point(text="Conditions are improving [F7].", cites=["F7"]),
        ],
        hotspot_points=[Point(text="Ward 12 is the hotspot [F1].", cites=["F1"])],
        cannot_tell=["No data after 2026-11-03."],
    )
    issues = check_object(interpretation, findings)
    assert "unsupported_number" in kinds(issues)
    assert "unknown_citation" in kinds(issues)
    # The valid headline and hotspot point raise nothing.
    assert not any("averaged 30" in issue.sentence for issue in issues)
    assert not any("is the hotspot" in issue.sentence for issue in issues)


def test_supported_object_has_no_issues():
    findings = make_findings()
    interpretation = Interpretation(
        headline="Ward 12 stays the hotspot at AQI 180 [F1].",
        trend_points=[Point(text="The AQI is falling by 2.5 points a day [F2].", cites=["F2"])],
        hotspot_points=[Point(text="Reliability is 0.82, so read it with care [F1].", cites=["F1"])],
        cannot_tell=["No data after 2026-11-03."],
    )
    assert check_object(interpretation, findings) == []


# ---------------------------------------------------------------- any language
def test_devanagari_digits_are_checked_like_ascii_digits():
    findings = make_findings()
    # 180 is a fact (F1), so with a citation the sentence is clean.
    assert check_text("यह स्टेशन का AQI १८० था [F1]।", findings) == []
    issues = check_text("हवा की गुणवत्ता ३०५ है।", findings)
    assert "uncited_claim" in kinds(issues)
    assert any("305" in issue.detail for issue in issues)


def test_devanagari_danda_ends_a_sentence():
    findings = make_findings()
    issues = check_text("पहला वाक्य ३०५ है। दूसरा वाक्य [F9]।", findings)
    assert "unknown_citation" in kinds(issues)
    sentences = {issue.sentence for issue in issues if issue.kind == "uncited_claim"}
    assert any("पहला" in sentence for sentence in sentences)
    # The second sentence cites an unknown id, so it must not be merged into the first.
    assert any("दूसरा" in issue.sentence for issue in issues if issue.kind == "unknown_citation")


def test_other_indic_digit_sets_are_normalised_too():
    findings = make_findings()
    # Bengali ১৮০ and Kannada ೧೮೦ are both 180.
    assert check_text("AQI ১৮০ ছিল [F1]।", findings) == []
    assert check_text("AQI ೧೮೦ ಆಗಿತ್ತು [F1]।", findings) == []


def test_percentages_and_ranges_are_read_as_numbers():
    findings = [
        Finding(
            id="F1",
            type="pattern",
            title="PM2.5 peaks in the evening",
            facts={"share_pct": 85.0, "window_start": 18.0, "window_end": 22.0},
            formula="x",
            stations=["Ward 12"],
        )
    ]
    assert check_text("85% of readings fall in the 18-22 window [F1].", findings) == []
    assert check_text("85% of readings fall in the 18–22 window [F1].", findings) == []
    issues = check_text("50% of readings fall outside the window [F1].", findings)
    assert "unsupported_number" in kinds(issues)
    assert any("50" in issue.detail for issue in issues)


def test_citation_requirement_for_numbers_can_be_relaxed():
    findings = make_findings()
    strict = check_text("The AQI is 180 today.", findings)
    assert "uncited_claim" in kinds(strict)

    relaxed = check_text("The AQI is 180 today.", findings, require_citations_for_numbers=False)
    assert relaxed == []  # 180 is grounded; only the missing citation is excused

    # A number that is not grounded is still an unsupported number.
    issues = check_text("The AQI is 412 today.", findings, require_citations_for_numbers=False)
    assert kinds(issues) == {"unsupported_number"}

    hindi = check_text("आज AQI १८० है।", findings, require_citations_for_numbers=False)
    assert hindi == []


def test_claim_hints_still_need_a_citation_without_numbers():
    findings = make_findings()
    assert "uncited_claim" in kinds(check_text("The worst station is Ward 12.", findings))
