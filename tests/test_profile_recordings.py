"""VALR recordings import: Fathom REST and Fireflies GraphQL against fake responses, export files, cache."""
import json
import stat
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.profile import recordings

FIXTURES = Path(__file__).parent / "fixtures" / "profile"
SINCE = datetime(2026, 8, 17, tzinfo=timezone.utc)


def load(name):
    return (FIXTURES / name).read_bytes()


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": json.loads(body) if body else None})
        status, payload, headers_out = self.responses.pop(0)
        return recordings.HttpResponse(status, headers_out, payload)


def http(transport):
    return recordings.Http(transport=transport, sleep=lambda s: None)


def test_fathom_paginates_with_cursor_and_api_key_header(tmp_path):
    fake = FakeTransport([(200, load("fathom_page1.json"), {}),
                          (200, load("fathom_page2.json"), {})])
    cache = recordings.RawCache(tmp_path / "raw")
    items = recordings.fetch_fathom("secret-key", SINCE, http(fake), cache)
    assert [i["recording_id"] for i in items] == [111, 222]
    first = urllib.parse.urlsplit(fake.calls[0]["url"])
    query = urllib.parse.parse_qs(first.query)
    assert first.netloc == "api.fathom.ai" and first.path == "/external/v1/meetings"
    assert query["created_after"] == ["2026-08-17T00:00:00Z"]
    assert query["include_transcript"] == ["true"] and query["include_summary"] == ["true"]
    assert "cursor" not in query
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(fake.calls[1]["url"]).query)["cursor"] == ["c2"]
    assert fake.calls[0]["headers"]["X-Api-Key"] == "secret-key"
    assert stat.S_IMODE((tmp_path / "raw").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "raw" / "fathom" / "meetings-000.json").stat().st_mode) == 0o600


def test_fathom_normalisation_keeps_names_not_emails():
    item = json.loads(load("fathom_page1.json"))["items"][0]
    meeting = recordings.normalize_fathom(item)
    assert meeting["title"] == "Acme interview - system design round"
    assert meeting["duration"] == 45 * 60
    assert meeting["participants"] == ["Priya Interviewer", "Chirag Goel"]
    assert meeting["turns"][1] == {"speaker": "Chirag Goel", "start": 12.0, "text": (
        "Um so basically I would start with a token bucket per tenant and keep counters in "
        "Redis with a TTL.")}
    assert "@" not in json.dumps(meeting)
    assert meeting["vendor_summary"].startswith("System design")


def test_rate_limit_waits_retry_after_then_succeeds():
    waits = []
    fake = FakeTransport([(429, b"{}", {"Retry-After": "7"}), (200, load("fathom_page2.json"), {})])
    client = recordings.Http(transport=fake, sleep=waits.append)
    assert len(recordings.fetch_fathom("k", SINCE, client, None)) == 1
    assert 7.0 in waits


def test_http_error_never_includes_the_key():
    fake = FakeTransport([(401, b'{"error": "bad"}', {})])
    with pytest.raises(recordings.ApiError) as err:
        recordings.fetch_fathom("super-secret", SINCE, http(fake), None)
    assert "super-secret" not in str(err.value) and "401" in str(err.value)


def test_fireflies_lists_then_fetches_sentences_only_when_wanted(tmp_path):
    fake = FakeTransport([(200, load("fireflies_list.json"), {}),
                          (200, load("fireflies_transcript_ff1.json"), {})])
    raw = recordings.fetch_fireflies("ff-key", SINCE, http(fake), recordings.RawCache(tmp_path),
                                     want_sentences=lambda m: "standup" not in m["title"].lower())
    assert len(fake.calls) == 2  # list (fewer than 50 -> one page) + one transcript
    list_call, one_call = fake.calls
    assert list_call["url"] == "https://api.fireflies.ai/graphql"
    assert list_call["headers"]["Authorization"] == "Bearer ff-key"
    assert list_call["body"]["variables"] == {"fromDate": "2026-08-17T00:00:00Z",
                                              "limit": 50, "skip": 0}
    assert "email" not in list_call["body"]["query"].replace("displayName", "")
    assert "participants" not in list_call["body"]["query"]  # that field is emails
    assert one_call["body"]["variables"] == {"id": "ff1"}
    meetings = [recordings.normalize_fireflies(r) for r in raw]
    screen, standup = meetings
    assert screen["date"] == "2025-09-15T10:00:00Z" and screen["duration"] == 30.5 * 60
    assert screen["participants"] == ["Chirag", "Rahul Recruiter"]  # email-only attendee dropped
    assert [t["speaker"] for t in screen["turns"]] == ["Rahul Recruiter", "Chirag", "Chirag",
                                                      "Rahul Recruiter"]
    assert standup["turns"] == []


def test_fireflies_paginates_by_skip():
    page = {"data": {"transcripts": [{"id": str(i), "title": "Daily standup", "date": 0,
                                      "duration": 1} for i in range(50)]}}
    last = {"data": {"transcripts": []}}
    fake = FakeTransport([(200, json.dumps(page).encode(), {}), (200, json.dumps(last).encode(), {})])
    raw = recordings.fetch_fireflies("k", SINCE, http(fake), None, want_sentences=lambda m: False)
    assert len(raw) == 50
    assert [c["body"]["variables"]["skip"] for c in fake.calls] == [0, 50]


def test_fireflies_graphql_errors_raise():
    fake = FakeTransport([(200, b'{"errors": [{"message": "x", "extensions": {"code": "too_many_requests"}}]}', {})])
    with pytest.raises(recordings.ApiError, match="too_many_requests"):
        recordings.fetch_fireflies("k", SINCE, http(fake), None)


def test_export_file_formats(tmp_path):
    single = recordings.load_export(FIXTURES / "export_meeting.json")
    assert len(single) == 1
    m = single[0]
    assert m["source"] == "fathom" and m["duration"] == 50 * 60
    assert m["participants"] == ["Neha HM"]
    assert m["participant_roles"] == {"Neha HM": "Engineering Manager, Gamma"}
    assert m["vendor_summary"] == "Hiring manager round."
    (tmp_path / "a.json").write_text(json.dumps({"source": "fireflies", "meetings": [
        {"id": "1", "title": "t", "date": "2026-09-01T00:00:00Z", "duration_minutes": 10,
         "transcript": [{"speaker": "A", "text": "hi", "timestamp": "00:01:00"}]}]}))
    both = recordings.load_export(tmp_path)
    assert both[0]["source"] == "fireflies" and both[0]["turns"][0]["start"] == 60.0


def test_window_filter():
    assert recordings.in_window({"date": "2026-09-01T00:00:00Z"}, SINCE)
    assert not recordings.in_window({"date": "2026-08-01T00:00:00Z"}, SINCE)


def test_purge_removes_cache(tmp_path):
    cache = recordings.RawCache(tmp_path / "raw")
    cache.save("fathom", "x", {"a": 1})
    assert cache.purge() and not (tmp_path / "raw").exists()
    assert cache.purge() is False


def test_export_repairs_bad_escapes_and_skips_garbage(tmp_path):
    (tmp_path / "a.json").write_text('{"id": "1", "title": "x \\_y \\u00 a\\\\\\> b", "transcript": []}')
    (tmp_path / "b.json").write_text('{not json')
    meetings = recordings.load_export(tmp_path)
    assert [m["title"] for m in meetings] == ["x \\_y \\u00 a\\\\> b"]


def test_export_index_and_transcript_file_dedupe(tmp_path):
    (tmp_path / "index.json").write_text(json.dumps({"source": "fathom", "meetings": [
        {"source": "fathom", "id": "9", "title": "Round", "transcript": []},
        {"source": "fathom", "id": "10", "title": "Other", "transcript": []}]}))
    (tmp_path / "fathom_9.json").write_text(json.dumps(
        {"source": "fathom", "id": "9", "title": "Round",
         "transcript": [{"speaker": "A", "text": "hi"}]}))
    meetings = {m["id"]: m for m in recordings.load_export(tmp_path)}
    assert len(meetings) == 2 and len(meetings["9"]["turns"]) == 1
