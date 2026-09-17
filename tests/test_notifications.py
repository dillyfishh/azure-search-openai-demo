import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import ResourceNotFoundError, ServiceRequestError
from pydantic import ValidationError

from notifications.models import NotificationDocument
from notifications.service import (
    NotificationService,
    NotificationSettings,
    active_payload,
)


def document(*entries):
    return NotificationDocument.model_validate_json(json.dumps({"schemaVersion": 1, "notifications": entries}))


def entry(**kwargs):
    return {"message": "A message", **kwargs}


def instant(text="2030-09-20T20:00:00+00:00"):
    return datetime.fromisoformat(text)


def schedule(**kwargs):
    return {
        "startDate": "2030-09-20",
        "endDate": "2030-09-22",
        "startTime": "06:00:00",
        "endTime": "22:00:00",
        **kwargs,
    }


@pytest.mark.parametrize(
    "now,active",
    [
        ("2030-09-19T19:59:59.999999+00:00", False),
        ("2030-09-19T20:00:00+00:00", True),
        ("2030-09-20T14:00:00+00:00", True),  # Midnight Sydney: it stays on overnight.
        ("2030-09-21T03:00:00+00:00", True),
        ("2030-09-22T12:00:00+00:00", True),
        ("2030-09-22T12:00:00.000001+00:00", False),
    ],
)
def test_continuous_inclusive_window_defaults_to_sydney(now, active):
    doc = document(entry(schedule=schedule()))
    assert doc.notifications[0].schedule.timezone == "Australia/Sydney"
    assert bool(active_payload(doc, instant(now), 60)["notifications"]) == active


def test_custom_timezone():
    doc = document(entry(schedule=schedule(timezone="UTC")))
    assert not active_payload(doc, instant("2030-09-20T05:59:59Z"), 60)["notifications"]
    assert active_payload(doc, instant("2030-09-20T06:00:00Z"), 60)["notifications"]
    assert active_payload(doc, instant("2030-09-22T22:00:00Z"), 60)["notifications"]


def test_filters_sorts_and_returns_only_presentation_fields():
    doc = document(
        entry(message="future", schedule=schedule(startDate="2090-01-01", endDate="2090-01-02")),
        entry(message="disabled", enabled=False),
        entry(message="expired", schedule=schedule(startDate="2020-01-01", endDate="2020-01-02")),
        entry(message="low", priority=1),
        entry(message="first", priority=100),
        entry(message="second", priority=100),
    )
    payload = active_payload(doc, instant(), 60)
    assert [n["message"] for n in payload["notifications"]] == ["first", "second", "low"]
    assert set(payload["notifications"][0]) == {"key", "type", "title", "message", "dismissible"}
    assert payload["refreshAfterMs"] == 60000


def test_next_boundary_shortens_browser_lease():
    payload = active_payload(document(entry(schedule=schedule())), instant("2030-09-22T11:59:59Z"), 60)
    assert payload["validForMs"] == payload["refreshAfterMs"] == 1001


def test_editing_changes_dismissal_key_but_reordering_does_not():
    original = entry(dismissible=True)
    other = entry(message="Another message")
    first = active_payload(document(original, other), instant(), 60)["notifications"]
    reordered = active_payload(document(other, original), instant(), 60)["notifications"]
    assert first[0]["key"] == reordered[1]["key"]
    for change in (entry(message="Updated", dismissible=True), entry(dismissible=True, schedule=schedule())):
        assert active_payload(document(change), instant(), 60)["notifications"][0]["key"] != first[0]["key"]


def test_no_author_id_required_and_exact_duplicates_display_once():
    assert len(active_payload(document(entry(), entry()), instant(), 60)["notifications"]) == 1


def test_sydney_dst_fall_includes_both_occurrences():
    doc = document(
        entry(schedule=schedule(startDate="2030-04-07", endDate="2030-04-07", startTime="02:15:00", endTime="02:45:00"))
    )
    for now in ("2030-04-06T15:30:00Z", "2030-04-06T16:30:00Z"):
        assert active_payload(doc, instant(now), 60)["notifications"]


@pytest.mark.parametrize("boundary", ["startTime", "endTime"])
def test_nonexistent_dst_boundary_rejected(boundary):
    window = schedule(startDate="2030-10-06", endDate="2030-10-06", startTime="01:00:00", endTime="03:30:00")
    window[boundary] = "02:30:00"
    with pytest.raises(ValidationError, match="does not exist"):
        document(entry(schedule=window))


def test_message_limit_and_markdown():
    message = "See [More Information](https://moreinformation.com) **here**."
    assert document(entry(message=message)).notifications[0].message == message
    assert len(document(entry(message="x" * 40000)).notifications[0].message) == 40000
    with pytest.raises(ValidationError):
        document(entry(message="x" * 40001))


@pytest.mark.parametrize(
    "overrides",
    [
        {"type": "danger"},
        {"enabled": "false"},
        {"message": "  "},
        {"unknown": 1},
        {"priority": 101},
        {"id": "old-id"},
        {"revision": 1},
        {"action": {"label": "More", "url": "https://example.com"}},
        {"schedules": []},
        {"schedule": schedule(timezone="Not/AZone")},
        {"schedule": schedule(timezone="/etc/passwd")},
        {"schedule": schedule(endDate="2020-01-01")},
        {"schedule": schedule(startTime="06:00:00Z")},
        {"schedule": schedule(startDate="2030-09-22", startTime="23:00:00")},
        {"schedule": schedule(endDate="9999-12-31")},
        {"schedule": {"startDate": "2030-09-20"}},
    ],
)
def test_invalid_entries_rejected(overrides):
    with pytest.raises(ValidationError):
        document(entry(**overrides))


def test_invalid_schema_version_rejected():
    with pytest.raises(ValidationError):
        NotificationDocument.model_validate_json('{"schemaVersion":2,"notifications":[]}')


def blob_client(raw=None):
    raw = raw if raw is not None else json.dumps({"schemaVersion": 1, "notifications": [entry()]}).encode()
    blob = AsyncMock()
    blob.get_blob_properties.return_value = SimpleNamespace(size=len(raw), etag='"v1"')
    blob.download_blob.return_value.readall.return_value = raw
    return blob


@pytest.mark.asyncio
async def test_cache_coalesces_requests_and_uses_etags():
    blob = blob_client()
    service = NotificationService(NotificationSettings(enabled=True), blob)
    results = await asyncio.gather(*(service.get_payload() for _ in range(20)))
    assert all(result["notifications"] for result in results)
    assert blob.get_blob_properties.await_count == 1
    service.next_refresh = 0
    await service.get_payload()
    assert blob.get_blob_properties.await_count == 2
    assert blob.download_blob.await_count == 1
    assert blob.download_blob.call_args.kwargs["match_condition"] == MatchConditions.IfNotModified
    await service.close()
    blob.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_storage_update_replaces_cache():
    blob = blob_client()
    service = NotificationService(NotificationSettings(enabled=True), blob)
    assert (await service.get_payload())["notifications"]
    service.next_refresh = 0
    blob.get_blob_properties.return_value.etag = '"v2"'
    blob.download_blob.return_value.readall.return_value = b'{"schemaVersion":1,"notifications":[]}'
    assert (await service.get_payload())["notifications"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ResourceNotFoundError(), ServiceRequestError("offline"), asyncio.TimeoutError()])
async def test_storage_failure_clears_cache_and_throttles_retry(error):
    blob = blob_client()
    service = NotificationService(NotificationSettings(enabled=True), blob)
    await service.get_payload()
    service.next_refresh = 0
    blob.get_blob_properties.side_effect = error
    for _ in range(3):
        assert (await service.get_payload())["notifications"] == []
    assert service.etag is None
    assert blob.get_blob_properties.await_count == 2


@pytest.mark.asyncio
async def test_malformed_document_rejected():
    service = NotificationService(NotificationSettings(enabled=True), blob_client(b'{"secret":"do not expose"}'))
    assert (await service.get_payload())["notifications"] == []


@pytest.mark.asyncio
async def test_document_larger_than_old_byte_limit_is_downloaded():
    raw = json.dumps(
        {"schemaVersion": 1, "notifications": [entry(title=str(i), message="x" * 40000) for i in range(8)]}
    ).encode()
    assert len(raw) > 262144
    blob = blob_client(raw)
    service = NotificationService(NotificationSettings(enabled=True), blob)
    assert len((await service.get_payload())["notifications"]) == 8
    assert "length" not in blob.download_blob.call_args.kwargs


@pytest.mark.asyncio
async def test_disabled_needs_no_storage():
    service = NotificationService(NotificationSettings())
    assert (await service.get_payload())["notifications"] == []
    await service.close()


def test_env_configuration(monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATIONS_STORAGE_ACCOUNT_URL", "https://notices.blob.core.windows.net/")
    assert NotificationSettings.from_env().account_url == "https://notices.blob.core.windows.net"
    monkeypatch.setenv("NOTIFICATIONS_POLL_SECONDS", "0")
    with pytest.raises(ValueError, match="NOTIFICATIONS_POLL_SECONDS"):
        NotificationSettings.from_env()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://example.com",
        "https://example.com/?sig=secret",
        "https://user@example.com",
        "https://example.com/container",
    ],
)
def test_invalid_account_url(monkeypatch, url):
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATIONS_STORAGE_ACCOUNT_URL", url)
    with pytest.raises(ValueError):
        NotificationSettings.from_env()


def test_dedicated_container_required(monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATIONS_STORAGE_ACCOUNT_URL", "https://example.com")
    monkeypatch.setenv("NOTIFICATIONS_STORAGE_CONTAINER", "content")
    monkeypatch.setenv("AZURE_STORAGE_CONTAINER", "content")
    with pytest.raises(ValueError, match="dedicated container"):
        NotificationSettings.from_env()


def test_example_and_generated_schema():
    assert NotificationDocument.model_validate_json(Path("docs/notifications/notifications.example.json").read_bytes())
    checked_in = json.loads(Path("docs/notifications/notifications.schema.json").read_text())
    checked_in.pop("$schema")
    assert checked_in == NotificationDocument.model_json_schema()


@pytest.mark.asyncio
async def test_refresh_deadline():
    blob = blob_client()
    service = NotificationService(NotificationSettings(enabled=True, timeout_seconds=0.01), blob)

    async def never_finishes():
        await asyncio.Event().wait()

    blob.get_blob_properties.side_effect = never_finishes
    assert (await service.get_payload())["notifications"] == []
    assert service.next_refresh > 0


def test_unrecognized_enabled_value(monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "yes")
    with pytest.raises(ValueError, match="NOTIFICATIONS_ENABLED"):
        NotificationSettings.from_env()


def test_empty_container(monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATIONS_STORAGE_ACCOUNT_URL", "https://example.com")
    monkeypatch.setenv("NOTIFICATIONS_STORAGE_CONTAINER", "")
    with pytest.raises(ValueError, match="must not be empty"):
        NotificationSettings.from_env()


def test_client_construction_and_disabled_configuration(monkeypatch):
    from unittest.mock import Mock, patch

    credential = Mock()
    with patch("notifications.service.BlobClient") as create:
        service = NotificationService.create(
            NotificationSettings(enabled=True, account_url="https://example.com"), credential
        )
        assert service.blob is create.return_value
        assert create.call_args.kwargs["credential"] is credential
        assert create.call_args.kwargs["retry_total"] == 1
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "false")
    with patch("notifications.service.BlobClient") as create:
        assert NotificationService.create(NotificationSettings.from_env(), credential).blob is None
        create.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_refresh_is_noop():
    service = NotificationService(NotificationSettings())
    await service.refresh()
    assert service.document is None


@pytest.mark.parametrize("operation", ["validate", "schema", "invalid", "missing"])
def test_notification_authoring_cli(monkeypatch, tmp_path, capsys, operation):
    import runpy

    path = tmp_path / "notifications.json"
    if operation == "validate":
        path.write_text('{"schemaVersion":1,"notifications":[]}')
    elif operation == "invalid":
        path.write_text('{"schemaVersion":2,"notifications":[]}')
    args = ["notifications.validate", str(path)] + (["--write-schema"] if operation == "schema" else [])
    monkeypatch.setattr("sys.argv", args)
    if operation in ("invalid", "missing"):
        with pytest.raises(SystemExit, match="1"):
            runpy.run_module("notifications.validate", run_name="__main__")
        assert "Invalid notification document" in capsys.readouterr().err
    else:
        runpy.run_module("notifications.validate", run_name="__main__")
        if operation == "schema":
            assert json.loads(path.read_text())["$schema"].endswith("2020-12/schema")
        else:
            assert "Valid: 0 notification(s)" in capsys.readouterr().out
