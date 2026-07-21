from src.l2_interfaces.host.os.skills.coding_plan_quality import grade_coding_plan


def requirement(identifier, text):
    return {"id": identifier, "text": text}


def step(identifier, title, requirement_ids, depends_on=None):
    return {
        "id": identifier,
        "title": title,
        "requirement_ids": requirement_ids,
        "depends_on": depends_on or [],
    }


def test_strict_quality_accepts_compact_explicit_outcome_coverage():
    requirements = [
        requirement("req_1", "Ranges include their end value"),
        requirement("req_2", "Whitespace is accepted around range tokens"),
        requirement("req_3", "Descending ranges raise ValueError"),
    ]
    steps = [
        step(
            "implement",
            "Implement and verify the range behavior",
            ["req_1", "req_2", "req_3"],
        )
    ]

    first = grade_coding_plan(requirements, steps, "enforce")
    repeated = grade_coding_plan(requirements, steps, "enforce")

    assert first == repeated
    assert first["status"] == "pass"
    assert first["score"] == 100
    assert first["metrics"]["covered_requirement_count"] == 3
    assert len(first["graph_sha256"]) == 64
    assert len(first["report_sha256"]) == 64


def test_strict_quality_rejects_recorded_overplanned_shape_without_leaking_text():
    requirements = [
        requirement("req_1", "Ranges include their end value"),
        requirement("req_2", "Whitespace is accepted"),
        requirement("req_3", "Descending ranges raise ValueError"),
        requirement("req_4", "Repository verification passes"),
        requirement("req_5", "Plan steps and requirements are committed"),
    ]
    steps = [
        step("s1", "Inspect repository and locate implementation", []),
        step("s2", "Implement inclusive behavior", ["req_1"], ["s1"]),
        step("s3", "Run repository tests", ["req_2"], ["s2"]),
        step("s4", "Update plan evidence", ["req_3"], ["s3"]),
        step("s5", "Commit verified workspace", ["req_4", "req_5"], ["s4"]),
    ]

    report = grade_coding_plan(requirements, steps, "enforce")

    assert report["status"] == "reject"
    assert {item["code"] for item in report["findings"]} >= {
        "process_requirements",
        "orphan_steps",
        "bookkeeping_steps",
        "excessive_step_count",
        "fully_serialized_plan",
    }
    serialized = str(report)
    assert "Ranges include their end value" not in serialized
    assert "Inspect repository" not in serialized


def test_advisory_quality_preserves_legacy_unmapped_plan():
    report = grade_coding_plan(
        [requirement("req_1", "Behavior remains correct")],
        [step("implement", "Implement the change", [])],
        "advisory",
    )

    assert report["status"] == "advisory"
    assert not any(item["severity"] == "error" for item in report["findings"])
    assert {item["code"] for item in report["findings"]} == {
        "uncovered_requirements",
        "orphan_steps",
    }
