from app.schema import schema


def test_schema_has_expected_operations() -> None:
    sdl = str(schema)
    # Types
    assert "type Enrollment" in sdl
    assert "type TimelineSegment" in sdl
    assert "type FileJob" in sdl
    assert "type SearchResult" in sdl
    assert "input SearchFilter" in sdl
    assert "input Page" in sdl
    # Query fields
    for field in ("searchEnrollments", "memberByCard", "enrollmentTimeline", "fileJob", "employers"):
        assert field in sdl, f"missing {field}"
    # Mutations
    for field in ("terminateEnrollment", "addDependent", "replayFile"):
        assert field in sdl, f"missing {field}"


def test_search_filter_has_all_ui_fields() -> None:
    sdl = str(schema)
    for f in (
        "cardNumber",
        "firstName",
        "lastName",
        "ssnLast4",
        "employerName",
        "subgroupName",
        "planName",
        "planCode",
        "dob",
        "effectiveDateFrom",
        "effectiveDateTo",
        "terminationDateFrom",
        "terminationDateTo",
        "memberType",
        "status",
    ):
        assert f in sdl
