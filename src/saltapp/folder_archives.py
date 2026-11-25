# Folder archives (Salt 0.265.0), the agent side: file a folder's files away in named archives and take them out again.
# NEVER deletes a file: archiving is a label, restoring removes it. Twin of salt-agent-sdk 0.42.0's `client.folders`.
from __future__ import annotations

from typing import Any, TypedDict
from urllib.parse import urlencode

from saltapp.errors import SaltApiError

FOLDER_ARCHIVE_CODES = (
    "archive_required",
    "archive_name_required",
    "archive_invalid",
    "archive_not_found",
    "archive_forbidden",
    "archive_not_empty",
    "file_not_found",
    "message_ids_required",
    "too_many_files",
    "archived_invalid",
    "nothing_to_change",
)
ARCHIVE_FILES_MAX = 100


class _Unset:
    def __repr__(self) -> str:  # pragma: no cover
        return "UNSET"


# "Not given". An explicit None is a different thing: it sends `null`, which RESTORES (takes the file out of its archive).
# Giving neither `archive_id` nor `archive` is 422 `archive_required`.
UNSET: Any = _Unset()


class FolderArchive(TypedDict, total=False):
    id: str
    name: str
    note: str | None
    created_by_id: str | None
    created_at: str
    updated_at: str
    file_count: int


class ArchiveSpec(TypedDict, total=False):
    name: str  # at most 80
    note: str  # at most 500


class ArchiveFileResult(TypedDict, total=False):
    message_id: str
    status: str  # archived | cleared | unchanged | forbidden | not_found
    archive_id: str | None
    code: str
    error: str
    next_step: str


class ArchiveFilesResult(TypedDict, total=False):
    folder_id: str
    dry_run: bool
    archive: dict[str, Any]
    would_create_archive: dict[str, Any]
    counts: dict[str, int]
    files: list[ArchiveFileResult]


class FolderArchiveError(SaltApiError):
    """A refusal from a folder-archive call, typed: `.code` is one of `FOLDER_ARCHIVE_CODES`, `.sentence` the server's words and
    `.next_step` what to do next (the server's `next_step`). `archive_not_empty` (409) names the call that empties the archive."""

    def __init__(self, method: str, url: str, status: int, body: Any, retry_after: int | None = None) -> None:
        super().__init__(method, url, status, body, retry_after)
        b = body if isinstance(body, dict) else {}
        err = b.get("error")
        self.sentence: str = err if isinstance(err, str) else "Salt refused this archive call."
        step = b.get("next_step")
        self.next_step: str | None = step if isinstance(step, str) else None


def as_folder_archive_error(err: Any) -> FolderArchiveError | None:
    """`FolderArchiveError` for a `SaltApiError` whose code is one of `FOLDER_ARCHIVE_CODES`; None for anything else."""
    if isinstance(err, FolderArchiveError):
        return err
    if isinstance(err, SaltApiError) and err.code in FOLDER_ARCHIVE_CODES:
        return FolderArchiveError(err.method, err.url, err.status, err.body, err.retry_after)
    return None


Plan = tuple  # (method, path, body)


def _spec(archive_id: Any, archive: Any, dry_run: bool | None) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if archive_id is not UNSET:
        body["archive_id"] = archive_id
    if archive is not UNSET:
        body["archive"] = archive
    if dry_run is not None:
        body["dry_run"] = dry_run
    return body


class _FolderPlans:
    @staticmethod
    def list_files(folder_id: str, before: str | None, limit: int | None, archived: Any, archive_id: str | None) -> Plan:
        if isinstance(archived, bool):
            archived = "true" if archived else "false"
        params = [(k, str(v)) for k, v in (("before", before), ("limit", limit), ("archived", archived), ("archive_id", archive_id)) if v is not None]
        return ("GET", f"/api/v1/folders/{folder_id}/files" + (("?" + urlencode(params)) if params else ""), None)

    @staticmethod
    def list_archives(folder_id: str) -> Plan:
        return ("GET", f"/api/v1/folders/{folder_id}/archives", None)

    @staticmethod
    def update_archive(folder_id: str, archive_id: str, name: Any, note: Any) -> Plan:
        body: dict[str, Any] = {}
        if name is not UNSET:
            body["name"] = name
        if note is not UNSET:
            body["note"] = note  # None clears it
        return ("PATCH", f"/api/v1/folders/{folder_id}/archives/{archive_id}", body)

    @staticmethod
    def delete_archive(folder_id: str, archive_id: str) -> Plan:
        return ("DELETE", f"/api/v1/folders/{folder_id}/archives/{archive_id}", None)

    @staticmethod
    def archive_file(folder_id: str, message_id: str, archive_id: Any, archive: Any, dry_run: bool | None) -> Plan:
        return ("PUT", f"/api/v1/folders/{folder_id}/files/{message_id}/archive", _spec(archive_id, archive, dry_run))

    @staticmethod
    def archive_files(folder_id: str, message_ids: list[str], archive_id: Any, archive: Any, dry_run: bool | None) -> Plan:
        ids = list(message_ids)
        if len(ids) > ARCHIVE_FILES_MAX:
            path = f"/api/v1/folders/{folder_id}/files/archive"
            raise FolderArchiveError(
                "POST",
                path,
                422,
                {
                    "error": f"At most {ARCHIVE_FILES_MAX} files at a time.",
                    "code": "too_many_files",
                    "next_step": f"Send {ARCHIVE_FILES_MAX} message_ids or fewer per call and repeat.",
                },
            )
        return ("POST", f"/api/v1/folders/{folder_id}/files/archive", {"message_ids": ids, **_spec(archive_id, archive, dry_run)})


class FoldersApi:
    """`client.folders` on `SaltClient`: a shared folder's archives (`/api/v1/folders/:id/...`). Archiving files a file away under a
    name; it NEVER deletes. Restore (`unarchive_file(s)`) takes it out of its archive. `dry_run=True` reports what would happen and
    writes nothing, not even a new archive. A refusal raises `FolderArchiveError` (`.code`, `.sentence`, `.next_step`).

    Giving neither `archive_id` nor `archive` is 422 `archive_required`; passing `archive_id=None` explicitly sends `null`, which restores."""

    def __init__(self, client: Any) -> None:
        self._c = client

    def _run(self, api_key: str, plan: Plan) -> Any:
        method, path, body = plan
        try:
            return self._c._request(method, path, api_key, body)
        except SaltApiError as err:
            raise (as_folder_archive_error(err) or err) from None

    def list_files(self, api_key: str, folder_id: str, *, before: str | None = None, limit: int | None = None, archived: Any = None, archive_id: str | None = None) -> dict[str, Any]:
        """`GET /files`: `{folder, files, archives}`. `archived`: "all" (default), "true" (only archived) or "false"; `archive_id`: one archive's files."""
        return self._run(api_key, _FolderPlans.list_files(folder_id, before, limit, archived, archive_id))

    def list_archives(self, api_key: str, folder_id: str) -> dict[str, Any]:
        """`GET /archives`: `{folder_id, archives}`, newest first, with live file counts."""
        return self._run(api_key, _FolderPlans.list_archives(folder_id))

    def update_archive(self, api_key: str, folder_id: str, archive_id: str, *, name: Any = UNSET, note: Any = UNSET) -> dict[str, Any]:
        """`PATCH /archives/:id`: rename or re-describe (`note=None` clears it). Returns `{archive}`."""
        return self._run(api_key, _FolderPlans.update_archive(folder_id, archive_id, name, note))

    def delete_archive(self, api_key: str, folder_id: str, archive_id: str) -> dict[str, Any]:
        """`DELETE /archives/:id`: removes the label only, and only while empty (409 `archive_not_empty`). Never deletes a file."""
        return self._run(api_key, _FolderPlans.delete_archive(folder_id, archive_id))

    def archive_file(self, api_key: str, folder_id: str, message_id: str, *, archive_id: Any = UNSET, archive: Any = UNSET, dry_run: bool | None = None) -> dict[str, Any]:
        """`PUT /files/:message_id/archive`: file one file under `archive_id` or `archive={"name", "note"?}` (made if new). Idempotent
        (`unchanged`). Returns `{folder_id, dry_run, archive: {id, name, note, created}, file: {message_id, status, archive_id, code?, error?, next_step?}}`."""
        return self._run(api_key, _FolderPlans.archive_file(folder_id, message_id, archive_id, archive, dry_run))

    def archive_files(self, api_key: str, folder_id: str, message_ids: list[str], *, archive_id: Any = UNSET, archive: Any = UNSET, dry_run: bool | None = None) -> ArchiveFilesResult:
        """`POST /files/archive` for up to 100 files (more raises `too_many_files` before any request). Always 200 with a per-file
        `status` (archived, cleared, unchanged, forbidden, not_found): one refused file never blocks the rest."""
        return self._run(api_key, _FolderPlans.archive_files(folder_id, message_ids, archive_id, archive, dry_run))

    def unarchive_file(self, api_key: str, folder_id: str, message_id: str, *, dry_run: bool | None = None) -> dict[str, Any]:
        """Restore: `archive_file` with an explicit null. The file leaves its archive; nothing is deleted."""
        return self._run(api_key, _FolderPlans.archive_file(folder_id, message_id, None, UNSET, dry_run))

    def unarchive_files(self, api_key: str, folder_id: str, message_ids: list[str], *, dry_run: bool | None = None) -> ArchiveFilesResult:
        """Restore up to 100 files: `archive_files` with an explicit null."""
        return self._run(api_key, _FolderPlans.archive_files(folder_id, message_ids, None, UNSET, dry_run))


class AsyncFoldersApi:
    """`client.folders` on `AsyncSaltClient`: the same calls as `FoldersApi`, awaited."""

    def __init__(self, client: Any) -> None:
        self._c = client

    async def _run(self, api_key: str, plan: Plan) -> Any:
        method, path, body = plan
        try:
            return await self._c._request(method, path, api_key, body)
        except SaltApiError as err:
            raise (as_folder_archive_error(err) or err) from None

    async def list_files(self, api_key: str, folder_id: str, *, before: str | None = None, limit: int | None = None, archived: Any = None, archive_id: str | None = None) -> dict[str, Any]:
        """See FoldersApi.list_files."""
        return await self._run(api_key, _FolderPlans.list_files(folder_id, before, limit, archived, archive_id))

    async def list_archives(self, api_key: str, folder_id: str) -> dict[str, Any]:
        """See FoldersApi.list_archives."""
        return await self._run(api_key, _FolderPlans.list_archives(folder_id))

    async def update_archive(self, api_key: str, folder_id: str, archive_id: str, *, name: Any = UNSET, note: Any = UNSET) -> dict[str, Any]:
        """See FoldersApi.update_archive."""
        return await self._run(api_key, _FolderPlans.update_archive(folder_id, archive_id, name, note))

    async def delete_archive(self, api_key: str, folder_id: str, archive_id: str) -> dict[str, Any]:
        """See FoldersApi.delete_archive."""
        return await self._run(api_key, _FolderPlans.delete_archive(folder_id, archive_id))

    async def archive_file(self, api_key: str, folder_id: str, message_id: str, *, archive_id: Any = UNSET, archive: Any = UNSET, dry_run: bool | None = None) -> dict[str, Any]:
        """See FoldersApi.archive_file."""
        return await self._run(api_key, _FolderPlans.archive_file(folder_id, message_id, archive_id, archive, dry_run))

    async def archive_files(self, api_key: str, folder_id: str, message_ids: list[str], *, archive_id: Any = UNSET, archive: Any = UNSET, dry_run: bool | None = None) -> ArchiveFilesResult:
        """See FoldersApi.archive_files."""
        return await self._run(api_key, _FolderPlans.archive_files(folder_id, message_ids, archive_id, archive, dry_run))

    async def unarchive_file(self, api_key: str, folder_id: str, message_id: str, *, dry_run: bool | None = None) -> dict[str, Any]:
        """See FoldersApi.unarchive_file."""
        return await self._run(api_key, _FolderPlans.archive_file(folder_id, message_id, None, UNSET, dry_run))

    async def unarchive_files(self, api_key: str, folder_id: str, message_ids: list[str], *, dry_run: bool | None = None) -> ArchiveFilesResult:
        """See FoldersApi.unarchive_files."""
        return await self._run(api_key, _FolderPlans.archive_files(folder_id, message_ids, None, UNSET, dry_run))
