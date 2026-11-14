"""Tests for llm.ask: grounded answers, refusals, starter questions and the cache."""
from __future__ import annotations

import pytest

from core.models import (
    Brief,
    DiffResult,
    Finding,
    QualitySummary,
    StationAQI,
    StationChange,
    StationReliability,
)
from llm import ask, client, demo_cache, stages
from llm.schemas import AskAnswer


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEMO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(client, "MAX_ATTEMPTS", 1)
    monkeypatch.setattr(client, "BACKOFF_SECONDS", 0)


@pytest.fixture
def brief() -> Brief:
    quality = QualitySummary(
        n_rows_in=48,
        n_readings=44,
        flag_counts={"SENTINEL": 2, "STUCK": 1},
        stations=[
            StationReliability(
                station="Anand Vihar",
                n_readings=24,
                coverage=0.99,
                flagged_share=0.02,
                reliability=0.97,
                label="reliable",
            ),
            StationReliability(
                station="Shivajinagar",
                n_readings=20,
                coverage=0.4,
                flagged_share=0.3,
                reliability=0.28,
                label="unreliable",
                notes=["stuck sensor"],
            ),
        ],
        gaps=["Station Shivajinagar has no readings between 02:00 and 09:00 on 2026-11-03"],
    )
    findings = [
        Finding(
            id="F1",
            type="hotspot",
            title="Hotspot: Anand Vihar at AQI 214",
            facts={"aqi": 214, "category": "Poor", "dominant": "PM2.5"},
            formula="latest valid daily AQI per station",
            evidence_row_ids=["R1"],
            stations=["Anand Vihar"],
            confidence="high",
        ),
        Finding(
            id="F2",
            type="hotspot",
            title="Hotspot: Shivajinagar at AQI 132",
            facts={"aqi": 132, "category": "Moderately Polluted", "dominant": "PM10"},
            formula="latest valid daily AQI per station",
            evidence_row_ids=["R2"],
            stations=["Shivajinagar"],
            confidence="medium",
        ),
        Finding(
            id="F3",
            type="gap",
            title="Shivajinagar has a 7 hour data gap",
            facts={"gap_hours": 7},
            formula="expected minus received hourly readings",
            evidence_row_ids=[],
            stations=["Shivajinagar"],
            confidence="high",
        ),
        Finding(
            id="F4",
            type="pattern",
            title="Evening peak between 18:00 and 22:00, consistent with traffic exhaust",
            facts={"peak_hour": 20, "window_start": 18, "window_end": 22},
            formula="hour-of-day mean over the period",
            evidence_row_ids=["R5"],
            stations=["Anand Vihar"],
            confidence="medium",
        ),
    ]
    return Brief(
        brief_id="b1",
        created_at="2026-11-04T08:00:00",
        area="Pune",
        source_name="sample.csv",
        source_hash="abc123",
        period_start="2026-11-01",
        period_end="2026-11-03",
        station_aqi=[
            StationAQI(
                station="Anand Vihar",
                ward="Ward 4",
                aqi=214,
                category="Poor",
                dominant="PM2.5",
                latest_date="2026-11-03",
                reliability=0.97,
                confidence="high",
            ),
            StationAQI(
                station="Shivajinagar",
                ward="Ward 12",
                aqi=132,
                category="Moderately Polluted",
                dominant="PM10",
                latest_date="2026-11-03",
                reliability=0.28,
                confidence="low",
            ),
        ],
        findings=findings,
        quality=quality,
    )


@pytest.fixture
def diff() -> DiffResult:
    return DiffResult(
        has_previous=True,
        previous_brief_id="b0",
        station_changes=[
            StationChange(
                station="Anand Vihar",
                aqi_prev=180,
                aqi_now=214,
                delta=34,
                category_prev="Moderately Polluted",
                category_now="Poor",
                direction="worse",
            )
        ],
        new_hotspots=["Anand Vihar"],
        category_transitions=["Anand Vihar: Moderately Polluted -> Poor"],
        summary_facts={"n_worse": 1, "n_better": 0, "mean_delta": 34},
    )


# ------------------------------------------------------------------ starter questions


@pytest.mark.parametrize("persona", ["ward_officer", "housing_society", "school"])
def test_starter_questions_are_grounded_and_persona_specific(brief, diff, persona):
    questions = ask.starter_questions(brief, diff, persona)

    assert 4 <= len(questions) <= 5
    assert len(set(questions)) == len(questions)
    joined = " ".join(questions)
    assert "Anand Vihar" in joined or "Shivajinagar" in joined
    assert any(question.endswith("?") for question in questions)


def test_school_questions_ask_about_outdoor_activity(brief, diff):
    joined = " ".join(ask.starter_questions(brief, diff, "school")).lower()
    assert "sport" in joined or "outdoor" in joined or "assembly" in joined


def test_ward_officer_questions_name_a_station_and_trust(brief, diff):
    joined = " ".join(ask.starter_questions(brief, diff, "ward_officer"))
    assert "Anand Vihar" in joined
    assert "Shivajinagar" in joined  # the unreliable station


# ------------------------------------------------------------------ offline answers


def test_offline_answer_for_a_station_cites_that_station(brief, diff):
    result = ask.answer("Why is Anand Vihar the highest station?", brief, diff, "ward_officer")

    assert isinstance(result, AskAnswer)
    assert result.used_fallback is True
    assert result.answerable is True
    assert result.cites
    assert set(result.cites) <= {finding.id for finding in brief.findings}
    assert "Anand Vihar" in result.answer
    assert "[F" in result.answer
    assert result.verification["remaining"] == 0


def test_offline_answer_for_trust_cites_the_data_quality_finding(brief, diff):
    result = ask.answer("Can I trust Shivajinagar's numbers?", brief, diff, "school")

    assert result.answerable is True
    assert "F3" in result.cites
    assert "Shivajinagar" in result.answer



def test_offline_outdoor_question_leads_with_category_guidance(brief, diff):
    result = ask.answer("Can we hold outdoor sports at school this week?", brief, diff, "school")

    assert result.answerable is True
    assert result.answer.startswith("Move outdoor games and sport indoors at school")
    assert "18:00-22:00" in result.answer  # the peak window from the pattern finding
    assert result.verification["remaining"] == 0

def test_offline_answer_for_change_mentions_the_diff(brief, diff):
    result = ask.answer("What changed since the last brief?", brief, diff, "ward_officer")

    assert result.answerable is True
    assert "Since the last brief" in result.answer
    assert "[F" in result.answer
    assert set(result.cites) <= {finding.id for finding in brief.findings}


def test_medical_question_is_refused_politely(brief, diff):
    result = ask.answer("What dose of inhaler should my son take?", brief, diff, "school")

    assert result.answerable is False
    assert result.cites == []
    assert "doctor" in result.answer.lower()
    assert "diagnos" in result.answer.lower()


def test_out_of_scope_question_says_insufficient_evidence(brief, diff):
    result = ask.answer("Will it rain tomorrow in Mumbai?", brief, diff, "housing_society")

    assert result.answerable is False
    assert "insufficient evidence" in result.answer.lower()
    assert result.cites == []


def test_answer_never_raises_on_empty_question(brief, diff):
    result = ask.answer("", brief, diff, "ward_officer")
    assert result.answer
    assert result.answerable is False


def test_answer_on_a_brief_with_no_findings(brief, diff):
    empty = brief.model_copy(update={"findings": [], "station_aqi": []})
    result = ask.answer("Can we hold sports day?", empty, diff, "school")

    assert result.answer
    assert result.answerable is False
    assert result.cites == []


# ------------------------------------------------------------------ LLM path


def test_llm_answer_keeps_only_real_citations(monkeypatch, brief, diff):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    answer = AskAnswer(
        answer="Anand Vihar is Poor at AQI 214 [F1]. The gap makes Shivajinagar less certain [F3][F99].",
        cites=["F1", "F3", "F99"],
        answerable=True,
        follow_ups=["What changed since the last brief?"],
    )
    monkeypatch.setattr(client, "generate_json", lambda *a, **k: answer)

    result = ask.answer("Why is Anand Vihar the worst station?", brief, diff, "ward_officer")

    assert result.used_fallback is False
    assert result.cites == ["F1", "F3"]
    assert "[F99]" not in result.answer


def test_llm_answer_with_no_citation_falls_back(monkeypatch, brief, diff):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    answer = AskAnswer(
        answer="The air is bad because of traffic.",
        cites=[],
        answerable=True,
        follow_ups=[],
    )
    monkeypatch.setattr(client, "generate_json", lambda *a, **k: answer)

    result = ask.answer("Why is the air bad?", brief, diff, "ward_officer")

    assert result.used_fallback is True  # the unsupported answer is rejected, not shipped
    assert result.cites


def test_llm_answer_repair_loop_records_verification(monkeypatch, brief, diff):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    bad = AskAnswer(
        answer="Anand Vihar is at AQI 999 [F1].",
        cites=["F1"],
        answerable=True,
        follow_ups=[],
    )
    fixed = AskAnswer(
        answer="Anand Vihar is the worst station at AQI 214 [F1].",
        cites=["F1"],
        answerable=True,
        follow_ups=[],
    )
    calls: list[object] = []

    def fake(prompt, system, schema, temperature=0.2):
        calls.append(schema)
        return bad if len(calls) == 1 else fixed

    monkeypatch.setattr(client, "generate_json", fake)

    result = ask.answer("How bad is Anand Vihar?", brief, diff, "ward_officer")

    assert "999" not in result.answer
    assert result.verification["issues_found"] >= 1
    assert result.verification["repaired"] >= 1
    assert result.verification["remaining"] == 0


def test_answer_cache_replays_on_a_second_brief(monkeypatch, brief, diff):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    answer = AskAnswer(
        answer="Anand Vihar leads at AQI 214 [F1].",
        cites=["F1"],
        answerable=True,
        follow_ups=[],
    )
    monkeypatch.setattr(client, "generate_json", lambda *a, **k: answer)
    ask.answer("How bad is Anand Vihar?", brief, diff, "ward_officer")

    monkeypatch.delenv("GEMINI_API_KEY")
    later = brief.model_copy(update={"brief_id": "other", "created_at": "2030-01-01T00:00:00"})
    later_diff = diff.model_copy(update={"previous_brief_id": "b-999"})
    replayed = ask.answer("How bad is Anand Vihar?", later, later_diff, "ward_officer")

    assert replayed.used_fallback is False
    assert replayed.answer == answer.answer


def test_answer_cache_key_includes_the_question(brief, diff):
    first = stages.stage_key(
        "ask",
        "p7_ask",
        "AskAnswer",
        stages.brief_fingerprint(brief),
        stages.diff_fingerprint(diff),
        {"question": "how bad is anand vihar?", "persona": "ward_officer"},
    )
    second = stages.stage_key(
        "ask",
        "p7_ask",
        "AskAnswer",
        stages.brief_fingerprint(brief),
        stages.diff_fingerprint(diff),
        {"question": "can we hold sports day?", "persona": "ward_officer"},
    )
    assert first != second
    assert demo_cache.load_run(first) is None
