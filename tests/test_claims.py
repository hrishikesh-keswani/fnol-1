"""Claims fetch retry behaviour."""
from fnol_rules.claims import fetch_claims


def test_success_first_attempt():
    payload = {
        "source_id": "AUTO-D01-CLAIMS",
        "existing_claims": [],
        "intake_deliveries": 1,
        "loss_reference": "auto-loss-D01",
    }

    def get(_url):
        return 200, payload

    result = fetch_claims("AUTO-D01", "http://127.0.0.1:8767", get=get)
    assert result.snapshot.available is True
    assert result.snapshot.intake_deliveries == 1
    assert result.raw_json == payload
    assert len(result.attempts) == 1
    assert result.attempts[0].status == 200


def test_one_timeout_then_success():
    calls = {"n": 0}
    payload = {"existing_claims": [], "intake_deliveries": 1, "source_id": "X"}

    def get(_url):
        calls["n"] += 1
        if calls["n"] == 1:
            return 503, {"error": "upstream_timeout", "retryable": True}
        return 200, payload

    result = fetch_claims("AUTO-D11", "http://example", get=get)
    assert result.snapshot.available is True
    assert len(result.attempts) == 2
    assert [a.status for a in result.attempts] == [503, 200]


def test_always_timeout_after_three_attempts():
    calls = {"n": 0}

    def get(_url):
        calls["n"] += 1
        return 503, {"error": "upstream_timeout", "retryable": True}

    result = fetch_claims("AUTO-D07", "http://example", get=get)
    assert result.snapshot.available is False
    assert result.raw_json is None
    assert len(result.attempts) == 3
    assert calls["n"] == 3
    assert all(a.status == 503 for a in result.attempts)


def test_server_error_is_retried():
    calls = {"n": 0}

    def get(_url):
        calls["n"] += 1
        if calls["n"] == 1:
            return 500, {"error": "http_500"}
        return 200, {"existing_claims": [], "intake_deliveries": 1, "source_id": "X"}

    result = fetch_claims("AUTO-D01", "http://example", get=get)
    assert calls["n"] == 2
    assert result.snapshot.available is True


def test_not_found_is_not_retried():
    calls = {"n": 0}

    def get(_url):
        calls["n"] += 1
        return 404, {"error": "not_found"}

    result = fetch_claims("AUTO-D01", "http://example", get=get)
    assert calls["n"] == 1
    assert result.snapshot.available is False
    assert result.raw_json is None


def test_malformed_success_is_unavailable():
    def get(_url):
        return 200, {"existing_claims": "nope", "intake_deliveries": "2"}

    result = fetch_claims("AUTO-D01", "http://example", get=get)
    assert result.snapshot.available is False
    assert result.raw_json is None
    assert len(result.attempts) == 1


def test_slash_in_case_id_is_rejected():
    try:
        fetch_claims("AUTO/D01", "http://example", get=lambda _url: (200, {}))
    except ValueError as exc:
        assert "case_id" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_max_attempts_respected():
    calls = {"n": 0}

    def get(_url):
        calls["n"] += 1
        return 503, {"error": "upstream_timeout"}

    fetch_claims("X", "http://example", max_attempts=3, get=get)
    assert calls["n"] == 3
