"""Contract tests for the LLM layer: cache keys, closed schemas, code-owned category and
numbers, the check -> repair loop, prompt architecture and the offline eval floors."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.models import (
    Brief,
    DiffResult,
    Finding,
    QualitySummary,
    StationAQI,
    StationChange,
    StationReliability,
)
from llm import client, demo_cache, eval as eval_module, stages
from llm.schemas import (
    Action,
    ActionPlan,
    Advisory,
    CareBlock,
    InternalNote,
    Interpretation,
    Point,
    make_action_plan_schema,
    make_advisory_schema,
    make_ask_schema,
    make_interpretation_schema,
)


# ------------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """No key, no retries, cache in a temp dir."""
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
        flag_counts={"SENTINEL": 2, "STUCK": 1, "UNIT_AMBIGUOUS": 30},
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
        cleaning_log=["Converted NO2 from ppb to ug/m3 at one station"],
    )
    findings = [
        Finding(
            id="F1",
            type="hotspot",
            title="Hotspot: Anand Vihar at AQI 214",
            facts={"aqi": 214, "category": "Poor", "dominant": "PM2.5", "ward": "Ward 4"},
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
            caveats=["station flagged unreliable"],
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
        Finding(
            id="F5",
            type="exceedance",
            title="PM2.5 is 3.4 times the WHO 24-hour guideline",
            facts={"who_multiple": 3.4, "who_24h_limit": 15},
            formula="24-hour mean divided by the WHO 24-hour guideline",
            evidence_row_ids=["R2", "R3"],
            stations=["Anand Vihar"],
            confidence="high",
        ),
    ]
    station_aqi = [
        StationAQI(
            station="Anand Vihar",
            ward="Ward 4",
            aqi=214,
            category="Poor",
            dominant="PM2.5",
            latest_date="2026-11-03",
            n_valid_days=3,
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
            n_valid_days=2,
            reliability=0.28,
            confidence="low",
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
        station_aqi=station_aqi,
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


class _HttpError(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status_code = status


def _same_data(brief: Brief) -> Brief:
    """The same data loaded on another start: different id and timestamp."""
    return brief.model_copy(
        update={"brief_id": "B20261120T101010Z-ffff", "created_at": "2026-11-20T10:10:10+00:00"}
    )


# ------------------------------------------------------------------ cache fingerprint (bug 1)


def test_cache_key_ignores_brief_id_and_created_at(brief, diff):
    first = stages.stage_key(
        "interpret",
        "p2_interpreter",
        "Interpretation",
        stages.brief_fingerprint(brief),
        stages.diff_fingerprint(diff),
    )
    later = stages.stage_key(
        "interpret",
        "p2_interpreter",
        "Interpretation",
        stages.brief_fingerprint(_same_data(brief)),
        stages.diff_fingerprint(diff.model_copy(update={"previous_brief_id": "b999"})),
    )
    assert first == later


def test_cache_key_changes_with_data_and_params(brief, diff):
    base = stages.stage_key(
        "interpret",
        "p2_interpreter",
        "Interpretation",
        stages.brief_fingerprint(brief),
        stages.diff_fingerprint(diff),
    )
    changed = brief.model_copy(
        update={"findings": [brief.findings[0].model_copy(update={"confidence": "low"})]}
    )
    assert (
        stages.stage_key(
            "interpret",
            "p2_interpreter",
            "Interpretation",
            stages.brief_fingerprint(changed),
            stages.diff_fingerprint(diff),
        )
        != base
    )
    assert (
        stages.stage_key(
            "interpret",
            "p2_interpreter",
            "Interpretation",
            stages.brief_fingerprint(brief),
            stages.diff_fingerprint(diff),
            {"persona": "school"},
        )
        != base
    )
    assert demo_cache.run_key("interpret", "p2.v1", "Interpretation", {"a": 1}) != demo_cache.run_key(
        "interpret", "p2.v2", "Interpretation", {"a": 1}
    )


def test_cache_key_ignores_volatile_nested_fields():
    payload = {"brief": {"brief_id": "a", "created_at": "1", "findings": [{"aqi": 200}]}}
    other = {"brief": {"brief_id": "b", "created_at": "2", "findings": [{"aqi": 200}]}}
    assert demo_cache.make_key("stage", payload) == demo_cache.make_key("stage", other)
    assert demo_cache.strip_volatile({"brief_id": "x", "keep": 1}) == {"keep": 1}


def test_second_brief_from_the_same_data_replays_the_cached_run(monkeypatch, brief, diff):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    answer = Interpretation(
        headline="Anand Vihar is the worst station [F1].",
        trend_points=[Point(text="Evening peak is worst [F4].", cites=["F4"])],
        hotspot_points=[Point(text="Anand Vihar leads at AQI 214 [F1].", cites=["F1"])],
        cannot_tell=["Shivajinagar data is thin [F3]."],
    )
    monkeypatch.setattr(client, "generate_json", lambda *a, **k: answer)

    first = stages.interpret(brief, diff)
    assert first.used_fallback is False

    # A new app start: same data, new brief id and created_at, no API key.
    monkeypatch.delenv("GEMINI_API_KEY")
    later = _same_data(brief)
    later_diff = diff.model_copy(update={"previous_brief_id": "b-other"})

    replayed = stages.interpret(later, later_diff)

    assert replayed.used_fallback is False
    assert replayed.headline == answer.headline
    assert replayed.trend_points[0].text == "Evening peak is worst [F4]."


def test_changed_data_does_not_replay(monkeypatch, brief, diff):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    answer = Interpretation(
        headline="Anand Vihar is the worst station [F1].",
        trend_points=[],
        hotspot_points=[Point(text="Anand Vihar leads at AQI 214 [F1].", cites=["F1"])],
        cannot_tell=[],
    )
    monkeypatch.setattr(client, "generate_json", lambda *a, **k: answer)
    stages.interpret(brief, diff)

    monkeypatch.delenv("GEMINI_API_KEY")
    changed = brief.model_copy(
        update={"station_aqi": [brief.station_aqi[0].model_copy(update={"aqi": 300})]}
    )
    result = stages.interpret(changed, diff)
    assert result.used_fallback is True


# ------------------------------------------------------------------ closed schemas


def test_interpretation_schema_rejects_unknown_finding_id(brief):
    schema = make_interpretation_schema(["F1", "F2"])
    good = schema.model_validate(
        {
            "headline": "Anand Vihar is worst [F1].",
            "trend_points": [{"text": "Rising [F2].", "cites": ["F2"]}],
            "hotspot_points": [],
            "cannot_tell": [],
        }
    )
    assert good.trend_points[0].cites == ["F2"]
    with pytest.raises(ValidationError):
        schema.model_validate(
            {
                "headline": "Anand Vihar is worst [F9].",
                "trend_points": [{"text": "Rising [F9].", "cites": ["F9"]}],
                "hotspot_points": [],
                "cannot_tell": [],
            }
        )


def test_action_plan_schema_closes_library_ids_and_citations():
    schema = make_action_plan_schema(["F1"], ["wo-sweep-1"])
    action = {
        "rank": 1,
        "action": "Sweep the market road.",
        "why_now": "Anand Vihar is Poor [F1].",
        "cites": ["F1"],
        "owner": "ward officer",
        "effort": "low",
        "time_window": "today",
        "data_gap_action": "",
        "library_id": "wo-sweep-1",
    }
    schema.model_validate({"actions": [action]})
    with pytest.raises(ValidationError):
        schema.model_validate({"actions": [{**action, "library_id": "invented-action"}]})
    with pytest.raises(ValidationError):
        schema.model_validate({"actions": [{**action, "cites": ["F7"]}]})


def test_advisory_schema_leaves_code_owned_fields_out(brief):
    schema = make_advisory_schema(["F1"])
    properties = schema.model_json_schema()["properties"]
    assert "category" not in properties
    assert "language" not in properties
    assert "reading_level" not in properties


def test_ask_schema_closes_citations():
    schema = make_ask_schema(["F1", "F3"])
    schema.model_validate({"answer": "Thin data [F3].", "cites": ["F3"], "answerable": True})
    with pytest.raises(ValidationError):
        schema.model_validate({"answer": "Thin data [F4].", "cites": ["F4"], "answerable": True})


def test_schema_400_retries_once_with_the_loose_schema(monkeypatch, brief, diff):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    calls: list[type] = []
    loose_answer = Interpretation(
        headline="Loose schema worked [F1].",
        trend_points=[Point(text="Peak is evening [F4].", cites=["F4"])],
        hotspot_points=[],
        cannot_tell=[],
    )

    def fake(prompt, system, schema, temperature=0.2):
        calls.append(schema)
        if len(calls) == 1:
            raise _HttpError(400)
        return loose_answer

    monkeypatch.setattr(client, "generate_json", fake)

    result = stages.interpret(brief, diff)

    assert result.headline == "Loose schema worked [F1]."
    assert len(calls) == 2
    assert calls[0] is not Interpretation  # the strict, id-constrained schema
    assert calls[1] is Interpretation  # post-validated loose fallback


# ------------------------------------------------------------------ category and numbers by code


def test_city_advisory_states_the_range_and_the_peak_window(brief):
    advisory = stages.write_advisory(brief, "English", "standard", "Pune")

    assert advisory.category == "Poor"  # worst station, set by code
    assert "214" in advisory.today_message
    assert "ranges from Moderately Polluted at Shivajinagar to Poor at Anand Vihar" in advisory.today_message
    assert "18:00" in advisory.care.older_adults and "22:00" in advisory.care.older_adults
    assert advisory.used_fallback is True


def test_station_advisory_uses_that_stations_numbers(brief):
    advisory = stages.write_advisory(brief, "English", "standard", "Pune", station="Shivajinagar")

    assert advisory.category == "Moderately Polluted"
    assert "132" in advisory.today_message
    assert "214" not in advisory.today_message
    assert "Anand Vihar" not in advisory.headline
    assert "Shivajinagar" in advisory.headline or "Shivajinagar" in advisory.source_line


def test_station_advisory_handles_a_station_without_aqi(brief):
    advisory = stages.write_advisory(brief, "English", "standard", "Pune", station="Nowhere")
    assert advisory.headline
    assert advisory.category in {"Poor", "insufficient evidence", "Data unavailable"}


def test_model_written_aqi_number_is_replaced_by_the_code_number(monkeypatch, brief):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    wrong = Advisory(
        language="English",
        reading_level="standard",
        headline="Anand Vihar is Poor today.",
        category="Poor",
        today_message="AQI 400 today, category Poor. Keep activity light [F1].",
        what_to_do=["Keep windows closed."],
        care=CareBlock(
            children="Keep play short.",
            older_adults="Keep trips short.",
            sensitive_groups="Keep medicine at hand.",
        ),
        source_line="Source: 2026-11-03.",
        cites=["F1"],
    )
    monkeypatch.setattr(client, "generate_json", lambda *a, **k: wrong)

    result = stages.write_advisory(brief, "English", "standard", "Pune")

    assert result.category == "Poor"
    assert "AQI 214" in result.today_message
    assert "400" not in result.today_message


def test_advisory_category_word_is_forced_in_the_headline(monkeypatch, brief):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    wrong = Advisory(
        language="English",
        reading_level="standard",
        headline="Air is Satisfactory today.",
        category="Satisfactory",
        today_message="The air is Satisfactory, so normal outdoor activity is fine [F1].",
        what_to_do=["Open the windows."],
        care=CareBlock(
            children="Children can play outside.",
            older_adults="Older adults can walk.",
            sensitive_groups="People with asthma can go out.",
        ),
        source_line="Source: 2026-11-03.",
        cites=["F1"],
    )
    monkeypatch.setattr(client, "generate_json", lambda *a, **k: wrong)

    result = stages.write_advisory(brief, "English", "standard", "Pune")

    assert result.category == "Poor"
    assert "Satisfactory" not in result.headline
    assert "Poor" in result.headline
    assert "outdoor activity is fine" not in result.today_message.lower()
    assert stages.category_contradictions(
        " ".join([result.today_message, *result.what_to_do, *stages._strings(result.care.model_dump())]),
        result.category,
    ) == []


# ------------------------------------------------------------------ check -> repair loop


def test_repair_call_fixes_the_named_sentence(monkeypatch, brief, diff):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    bad = Interpretation(
        headline="Anand Vihar is at AQI 999 [F1].",
        trend_points=[Point(text="Rising fast [F2].", cites=["F2"])],
        hotspot_points=[],
        cannot_tell=[],
    )
    fixed = Interpretation(
        headline="Anand Vihar is the worst station [F1].",
        trend_points=[Point(text="Rising fast [F2].", cites=["F2"])],
        hotspot_points=[],
        cannot_tell=[],
    )
    calls: list[object] = []

    def fake(prompt, system, schema, temperature=0.2):
        calls.append(schema)
        return bad if len(calls) == 1 else fixed

    monkeypatch.setattr(client, "generate_json", fake)

    result = stages.interpret(brief, diff)

    assert result.headline == "Anand Vihar is the worst station [F1]."
    assert result.verification["issues_found"] >= 1
    assert result.verification["remaining"] == 0
    assert result.verification["repaired"] >= 1
    assert len(calls) == 2  # one draft, one repair


def test_unrepaired_sentence_is_dropped(monkeypatch, brief, diff):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    bad = Interpretation(
        headline="Anand Vihar is the worst station [F1].",
        trend_points=[Point(text="Rising fast [F2].", cites=["F2"])],
        hotspot_points=[
            Point(text="A made-up number: 777 [F1].", cites=["F1"]),
            Point(text="Evening peak is worst [F4].", cites=["F4"]),
        ],
        cannot_tell=[],
    )
    # No repair ever succeeds: the client keeps returning the same bad answer.
    monkeypatch.setattr(client, "generate_json", lambda *a, **k: bad)

    result = stages.interpret(brief, diff)

    texts = [point.text for point in result.hotspot_points]
    assert all("777" not in text for text in texts)
    assert result.verification["remaining"] == 0


def test_offline_fallback_records_verification(brief, diff):
    result = stages.interpret(brief, diff)
    assert result.used_fallback is True
    assert set(("issues_found", "repaired", "remaining")) <= set(result.verification)
    assert result.verification["remaining"] == 0


def test_long_note_uses_the_p6_verifier(monkeypatch, brief, diff):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    note = InternalNote(markdown="## Situation\n" + ("Anand Vihar is Poor [F1]. " * 120))
    monkeypatch.setattr(client, "generate_json", lambda *a, **k: note)
    monkeypatch.setattr(stages, "P6_MIN_CHARS", 10)
    seen: list[str] = []

    def fake_verify(text, current):
        seen.append(text)
        return []

    monkeypatch.setattr(stages, "verify_draft", fake_verify)

    result = stages.write_internal_note(
        brief,
        diff,
        Interpretation(headline="h [F1].", trend_points=[], hotspot_points=[], cannot_tell=[]),
        ActionPlan(
            persona="ward_officer",
            actions=[
                Action(
                    rank=1,
                    action="Sweep the road [F1].",
                    why_now="Anand Vihar is Poor [F1].",
                    cites=["F1"],
                    owner="ward officer",
                    effort="low",
                    time_window="today",
                    data_gap_action="",
                    library_id="wo-sweep-1",
                )
            ],
        ),
    )

    assert seen, "P6 verifier should be called for a long draft"
    assert result.verification["p6_used"] is True


# ------------------------------------------------------------------ non-English checks


def test_non_english_number_needs_a_citation(brief):
    issues = stages.verification_issues(
        Advisory(
            language="Hindi",
            reading_level="simple",
            headline="आज हवा खराब है।",
            category="Poor",
            today_message="आज सूचकांक 214 है।",
            what_to_do=["खिड़कियाँ बंद रखें।"],
            care=CareBlock(children="बच्चे घर में रहें।", older_adults="बुज़ुर्ग घर में रहें।", sensitive_groups="दवा पास रखें।"),
            source_line="स्रोत: 2026-11-03।",
            cites=[],
        ),
        brief,
    )
    assert any("non-English" in issue.detail for issue in issues)


def test_non_english_cited_or_labelled_numbers_pass(brief):
    cited = Advisory(
        language="Hindi",
        reading_level="simple",
        headline="आज हवा खराब है।",
        category="Poor",
        today_message="आज सूचकांक 214 [F1] है।",
        what_to_do=["खिड़कियाँ बंद रखें।"],
        care=CareBlock(children="बच्चे घर में रहें।", older_adults="बुज़ुर्ग घर में रहें।", sensitive_groups="दवा पास रखें।"),
        source_line="स्रोत: 2026-11-03। स्टेशन: Anand Vihar",
        cites=["F1"],
    )
    issues = stages.verification_issues(cited, brief)
    assert [issue for issue in issues if issue.kind == "uncited_claim"] == []


def test_questions_and_library_ids_are_not_claims(brief):
    plan = ActionPlan(
        persona="ward_officer",
        actions=[
            Action(
                rank=1,
                action="Sweep the market road [F1].",
                why_now="Anand Vihar is Poor [F1].",
                cites=["F1"],
                owner="ward officer",
                effort="low",
                time_window="today",
                data_gap_action="",
                library_id="wo_misting_hotspot",
            )
        ],
    )
    assert stages.verification_issues(plan, brief) == []


# ------------------------------------------------------------------ prompt architecture


PROMPT_NAMES = (
    "p1_mapper",
    "p2_interpreter",
    "p3_actioner",
    "p4_internal_note",
    "p5_advisory",
    "p6_verifier",
    "p6b_repair",
    "p7_ask",
)


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_every_prompt_has_a_version_header_and_matches_prompt_versions(name):
    header = (stages.PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("prompt_version: ")
    assert header.split(":", 1)[1].strip() == stages.PROMPT_VERSIONS[name]
    assert stages.prompt_version(name) == stages.PROMPT_VERSIONS[name]


def test_contract_file_is_versioned_and_exposed():
    header = (stages.PROMPT_DIR / "grounding_contract.md").read_text(encoding="utf-8").splitlines()[0]
    assert header == f"prompt_version: {stages.PROMPT_VERSIONS['grounding_contract']}"


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_every_prompt_inlines_the_shared_contract(name):
    system, _user = stages.load_prompt(name)
    assert "Shared grounding contract" in system
    assert "Cite every claim" in system
    assert "{CONTRACT}" not in system


@pytest.mark.parametrize("name", PROMPT_NAMES)
def test_every_prompt_has_one_good_and_one_bad_example(name):
    system, user = stages.load_prompt(name)
    text = f"{system}\n{user}".lower()
    assert "example, good" in text
    assert "example, bad" in text


def test_p5_has_concrete_reading_levels_and_the_glossary_rule():
    system, _user = stages.load_prompt("p5_advisory")
    assert "12 words per sentence" in system
    assert "glossary is frozen" in system.lower()
    assert "what this means for you today" in system.lower()


def test_prompt_versions_covers_every_prompt_file():
    on_disk = {path.stem for path in stages.PROMPT_DIR.glob("*.md")}
    assert on_disk == set(stages.PROMPT_VERSIONS)


def test_load_prompt_rejects_a_missing_version_header(tmp_path, monkeypatch):
    monkeypatch.setattr(stages, "PROMPT_DIR", tmp_path)
    (tmp_path / "broken.md").write_text("## SYSTEM\nx\n## USER\ny\n", encoding="utf-8")
    stages.prompt_version.cache_clear()
    stages.load_prompt.cache_clear()
    try:
        with pytest.raises(ValueError):
            stages.prompt_version("broken")
    finally:
        stages.prompt_version.cache_clear()
        stages.load_prompt.cache_clear()


# ------------------------------------------------------------------ eval floors


def test_offline_eval_meets_the_floors():
    report = eval_module.run_eval()

    overall = report["overall"]
    assert overall["fault_recall"] >= eval_module.FLOORS["fault_recall"]
    assert overall["citation_validity"] >= eval_module.FLOORS["citation_validity"]
    assert overall["unsupported_numbers"] <= eval_module.FLOORS["unsupported_numbers"]
    assert overall["category_contradictions"] <= eval_module.FLOORS["category_contradictions"]
    assert overall["verification_remaining"] == 0


def test_manifest_parser_finds_the_planted_faults():
    manifest = eval_module.parse_manifest(
        (eval_module.DATA_DIR / "sample_manifest.md").read_text(encoding="utf-8")
    )
    assert manifest["week1"]["counts"]["SENTINEL"] == 8
    assert manifest["week1"]["counts"]["STUCK"] == 9
    # Week 2 lists 12 sentinel lines; one row appears twice (NO2 range + its CO cell),
    # so the line-count is one higher than the 29 SENTINEL cells the cleaner flags.
    assert manifest["week2"]["counts"]["SENTINEL"] == 30
    assert "Pashan" in manifest["week1"]["gap_stations"]
    assert "Nigdi" in manifest["week2"]["gap_stations"]
