# Salt 0.265.0 folder archives, agent side: client.folders (sync + async), FolderArchiveError, explicit-null restore.
# Mirrors salt-agent-sdk tests for 0.42.0. No network: httpx.MockTransport.
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from saltapp import FolderArchiveError, SaltApiError
from saltapp.client import AsyncSaltClient, SaltClient
from saltapp.folder_archives import FOLDER_ARCHIVE_CODES

HOST = "https://example.saltapp.test"


def sync_client(handler) -> SaltClient:
    return SaltClient(HOST, http=httpx.Client(transport=httpx.MockTransport(handler)))


def async_client(handler) -> AsyncSaltClient:
    return AsyncSaltClient(HOST, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def recorder(response):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(request) if callable(response) else response

    return calls, handler


def test_list_files_passes_the_filters():
    calls, handler = recorder(httpx.Response(200, json={"folder": {}, "files": [], "archives": []}))
    f = sync_client(handler).folders
    f.list_files("k", "f1")
    f.list_files("k", "f1", before="m9", limit=20, archived="false", archive_id="a1")
    f.list_files("k", "f1", archived=True)
    assert calls[0].url.path == "/api/v1/folders/f1/files" and dict(calls[0].url.params) == {}
    assert dict(calls[1].url.params) == {"before": "m9", "limit": "20", "archived": "false", "archive_id": "a1"}
    assert dict(calls[2].url.params) == {"archived": "true"}


def test_list_update_delete_archive():
    calls, handler = recorder(httpx.Response(200, json={}))
    f = sync_client(handler).folders
    f.list_archives("k", "f1")
    f.update_archive("k", "f1", "a1", name="2026", note=None)
    f.update_archive("k", "f1", "a1", note="why")
    f.delete_archive("k", "f1", "a1")
    assert [(c.method, c.url.path) for c in calls] == [
        ("GET", "/api/v1/folders/f1/archives"),
        ("PATCH", "/api/v1/folders/f1/archives/a1"),
        ("PATCH", "/api/v1/folders/f1/archives/a1"),
        ("DELETE", "/api/v1/folders/f1/archives/a1"),
    ]
    assert json.loads(calls[1].content) == {"name": "2026", "note": None}  # note None clears it
    assert json.loads(calls[2].content) == {"note": "why"}


def test_archive_file_by_id_or_by_name_and_dry_run():
    calls, handler = recorder(httpx.Response(200, json={"folder_id": "f1", "dry_run": True, "archive": {"id": "a1", "name": "Old", "note": None, "created": False}, "file": {"message_id": "m1", "status": "archived", "archive_id": "a1"}}))
    f = sync_client(handler).folders
    r = f.archive_file("k", "f1", "m1", archive_id="a1")
    f.archive_file("k", "f1", "m1", archive={"name": "Old", "note": "done"}, dry_run=True)
    assert r["file"]["status"] == "archived"
    assert calls[0].method == "PUT" and calls[0].url.path == "/api/v1/folders/f1/files/m1/archive"
    assert json.loads(calls[0].content) == {"archive_id": "a1"}
    assert json.loads(calls[1].content) == {"archive": {"name": "Old", "note": "done"}, "dry_run": True}


def test_explicit_null_restores_and_omitting_the_key_does_not():
    calls, handler = recorder(httpx.Response(200, json={}))
    f = sync_client(handler).folders
    f.unarchive_file("k", "f1", "m1")
    f.unarchive_file("k", "f1", "m1", dry_run=True)
    f.archive_file("k", "f1", "m1", archive_id=None)
    f.archive_file("k", "f1", "m1")  # neither given: the key is omitted (server: 422 archive_required)
    assert json.loads(calls[0].content) == {"archive_id": None}
    assert json.loads(calls[1].content) == {"archive_id": None, "dry_run": True}
    assert json.loads(calls[2].content) == {"archive_id": None}
    assert json.loads(calls[3].content) == {}
    assert calls[0].method == "PUT"


def test_archive_required_is_a_typed_error_with_next_step():
    body = {"error": "Say which archive.", "code": "archive_required", "next_step": "Send archive_id or archive, or null to restore."}
    _, handler = recorder(httpx.Response(422, json=body))
    with pytest.raises(FolderArchiveError) as exc:
        sync_client(handler).folders.archive_file("k", "f1", "m1")
    e = exc.value
    assert isinstance(e, SaltApiError) and e.code == "archive_required" and e.sentence == "Say which archive." and e.next_step.startswith("Send archive_id")


@pytest.mark.parametrize("code", FOLDER_ARCHIVE_CODES)
def test_every_archive_code_is_typed(code):
    _, handler = recorder(httpx.Response(409 if code == "archive_not_empty" else 422, json={"error": "No.", "code": code, "next_step": "Do this."}))
    with pytest.raises(FolderArchiveError) as exc:
        sync_client(handler).folders.delete_archive("k", "f1", "a1")
    assert exc.value.code == code and exc.value.next_step == "Do this."


def test_unrelated_errors_stay_plain():
    _, handler = recorder(httpx.Response(404, json={"error": "Not found."}))
    with pytest.raises(SaltApiError) as exc:
        sync_client(handler).folders.list_archives("k", "f1")
    assert not isinstance(exc.value, FolderArchiveError) and exc.value.next_step if hasattr(exc.value, "next_step") else True


def test_archive_files_batches_and_returns_per_file_results():
    result = {"folder_id": "f1", "dry_run": False, "archive": {"id": "a1", "name": "Old", "note": None, "created": True}, "counts": {"archived": 1, "forbidden": 1}, "files": [
        {"message_id": "m1", "status": "archived", "archive_id": "a1"},
        {"message_id": "m2", "status": "forbidden", "archive_id": None, "code": "archive_forbidden", "error": "Not yours.", "next_step": "Ask the owner."},
    ]}
    calls, handler = recorder(httpx.Response(200, json=result))
    f = sync_client(handler).folders
    r = f.archive_files("k", "f1", ["m1", "m2"], archive={"name": "Old"})
    assert r["counts"] == {"archived": 1, "forbidden": 1} and r["files"][1]["status"] == "forbidden"
    assert calls[0].method == "POST" and calls[0].url.path == "/api/v1/folders/f1/files/archive"
    assert json.loads(calls[0].content) == {"message_ids": ["m1", "m2"], "archive": {"name": "Old"}}
    f.unarchive_files("k", "f1", ["m1"], dry_run=True)
    assert json.loads(calls[1].content) == {"message_ids": ["m1"], "archive_id": None, "dry_run": True}


def test_archive_files_over_100_throws_before_any_request():
    calls, handler = recorder(httpx.Response(200, json={}))
    f = sync_client(handler).folders
    with pytest.raises(FolderArchiveError) as exc:
        f.archive_files("k", "f1", [str(i) for i in range(101)], archive_id="a1")
    assert exc.value.code == "too_many_files" and exc.value.next_step
    with pytest.raises(FolderArchiveError):
        f.unarchive_files("k", "f1", [str(i) for i in range(101)])
    assert calls == []
    f.archive_files("k", "f1", [str(i) for i in range(100)], archive_id="a1")
    assert len(calls) == 1


def test_async_namespace():
    async def run():
        calls, handler = recorder(lambda r: httpx.Response(422, json={"error": "x", "code": "archive_not_found", "next_step": "List archives."}) if r.url.path.endswith("/archives/zz") else httpx.Response(200, json={"ok": True}))
        f = async_client(handler).folders
        assert (await f.list_archives("k", "f1"))["ok"] is True
        await f.unarchive_file("k", "f1", "m1")
        assert json.loads(calls[1].content) == {"archive_id": None}
        await f.archive_files("k", "f1", ["m1"], archive_id="a1", dry_run=True)
        assert json.loads(calls[2].content) == {"message_ids": ["m1"], "archive_id": "a1", "dry_run": True}
        with pytest.raises(FolderArchiveError) as exc:
            await f.delete_archive("k", "f1", "zz")
        assert exc.value.code == "archive_not_found"
        with pytest.raises(FolderArchiveError):
            await f.archive_files("k", "f1", ["m"] * 101, archive_id="a1")

    asyncio.run(run())
