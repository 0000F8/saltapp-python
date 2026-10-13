"""Deterministic-first scheduled work ("script jobs").

A recurring job -- a news digest, a price watch, a "tell me when the build is
green" -- is mostly plumbing: fetch some pages, drop what was already sent,
write it up, post it. Only the "write it up" step ever needs a model, and
often not even that. The owner's rule (2026-10-13): "use the right tools for
the right jobs; don't use AI as the hammer for everything when deterministic
scripts and tools can accommodate just as well."

`run_script_job` is that rule as a function. The host's scheduler (cron, a
Grains wake_me, an asyncio loop) calls it; this module never schedules
anything.

  sources   plain functions (async or sync) that fetch and normalise items in
            code (no model). A synchronous source runs in a thread and must
            set its own network timeouts: wait_for abandons a hung thread, it
            cannot stop it. Each runs ONCE per call, with a timeout; one that
            raises or times out is listed in `failed_sources` and the rest
            carry on.
  key       a stable id for an item (its URL, a post id) -- what `seen`
            remembers.
  seen      a SeenStore (required): drops items already sent. `MemorySeenStore()` lives
            until the process restarts; `FileSeenStore(path)` survives it.
  compose   OPTIONAL, and the ONLY place a model may be called. It runs only
            when there is something new, so a quiet day costs no tokens. Use
            the cheapest model that can write the text. Leave it out and a
            plain list is posted (see `default_compose`). Return empty only
            for a transient failure: empty posts nothing and marks nothing,
            so the same items reach the model next run.
  post      sends the text to the chat.

Nothing new means no compose and no post. Items are marked seen only AFTER
`post` succeeds: a failed post leaves them unseen so the next run retries, and
a compose or post error is raised to the scheduler (re-arm the schedule in a
`finally` so a failed run never silently ends it).

Units differ from salt-agent-sdk on purpose: this package takes seconds
everywhere (`ttl_seconds`, `source_timeout_seconds`), the TypeScript one
milliseconds (`ttlMs`, `sourceTimeoutMs`).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol, Sequence, Union

_logger = logging.getLogger("saltapp.script_job")

#: A mark lasts 30 days unless the store or the call says otherwise.
DEFAULT_SEEN_TTL_SECONDS: float = 30 * 24 * 60 * 60
#: Per-source timeout when the caller does not pass one.
DEFAULT_SOURCE_TIMEOUT_SECONDS: float = 20.0


# --- SeenStore ---------------------------------------------------------------


class SeenStore(Protocol):
    """What a script job has already sent. Keys are opaque strings (a URL, a
    post id); each is remembered until its own expiry, so a feed that
    republishes an old story a month later can be told again on purpose."""

    async def has(self, key: str) -> bool:
        """True when `key` was marked and has not expired."""

    async def mark(self, keys: Sequence[str], *, ttl_seconds: Optional[float] = None) -> None:
        """Marks every key seen, each expiring `ttl_seconds` from now (the
        store's default when omitted). `float('inf')` never expires."""

    async def forget(self, key: str) -> None:
        """Forgets `key`, so the next run treats it as new. A no-op when it
        was never marked."""


def _expiry(now: float, ttl: Optional[float], default: float) -> Optional[float]:
    value = default if ttl is None else ttl
    return None if value == float("inf") else now + value


class MemorySeenStore:
    """In-memory SeenStore. Marks are lost on process restart, and it dedupes only while the SAME instance outlives the runs -- construct it once, not inside the job."""

    def __init__(self, *, ttl_seconds: float = DEFAULT_SEEN_TTL_SECONDS, now: Callable[[], float] = time.time) -> None:
        self._ttl = ttl_seconds
        self._now = now
        self._entries: dict[str, Optional[float]] = {}

    async def has(self, key: str) -> bool:
        if key not in self._entries:
            return False
        expires_at = self._entries[key]
        if expires_at is not None and expires_at <= self._now():
            del self._entries[key]
            return False
        return True

    async def mark(self, keys: Sequence[str], *, ttl_seconds: Optional[float] = None) -> None:
        at = self._now()
        for key in keys:
            self._entries[key] = _expiry(at, ttl_seconds, self._ttl)

    async def forget(self, key: str) -> None:
        self._entries.pop(key, None)


class FileSeenStore:
    """One plain JSON file, `{"<key>": <expires-at epoch seconds, or null for
    never>}`, written atomically (temp file + rename) so a crash mid-write
    never leaves half a file. Expired keys are dropped whenever the file is
    rewritten. A missing file is an empty store; an unreadable one raises
    rather than silently re-sending everything.

    The keys are whatever you pass as `key(item)` -- usually URLs -- stored in
    PLAINTEXT (the file is created 0600, its new directories 0700), so keep
    the file somewhere only this host can read. Run ONE job per file at a
    time: the lock is per instance, in this process only, so two processes (or
    two instances) on one file can lose each other's keys."""

    def __init__(self, path: str, *, ttl_seconds: float = DEFAULT_SEEN_TTL_SECONDS, now: Callable[[], float] = time.time) -> None:
        self._path = path
        self._ttl = ttl_seconds
        self._now = now
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Optional[float]]:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                parsed = json.load(fh)
        except FileNotFoundError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _save(self, entries: dict[str, Optional[float]]) -> None:
        directory = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=os.path.basename(self._path) + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(entries, fh, indent=2)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def _has(self, key: str) -> bool:
        with self._lock:
            entries = self._load()
        if key not in entries:
            return False
        expires_at = entries[key]
        return expires_at is None or expires_at > self._now()

    def _mark(self, keys: Sequence[str], ttl_seconds: Optional[float]) -> None:
        with self._lock:
            at = self._now()
            entries = {k: v for k, v in self._load().items() if v is None or v > at}
            for key in keys:
                entries[key] = _expiry(at, ttl_seconds, self._ttl)
            self._save(entries)

    def _forget(self, key: str) -> None:
        with self._lock:
            entries = self._load()
            if key in entries:
                del entries[key]
                self._save(entries)

    async def has(self, key: str) -> bool:
        return await asyncio.to_thread(self._has, key)

    async def mark(self, keys: Sequence[str], *, ttl_seconds: Optional[float] = None) -> None:
        await asyncio.to_thread(self._mark, list(keys), ttl_seconds)

    async def forget(self, key: str) -> None:
        await asyncio.to_thread(self._forget, key)


# --- run_script_job -----------------------------------------------------------

Source = Callable[[], Union[Sequence[Any], Awaitable[Sequence[Any]]]]
Compose = Callable[[list[Any]], Union[Optional[str], Awaitable[Optional[str]]]]
Post = Callable[[str, list[Any]], Union[Any, Awaitable[Any]]]


@dataclass
class ScriptJobResult:
    #: Items the sources returned, before dropping any (items from a failed source are not counted).
    fetched: int = 0
    #: Items not seen before and not duplicated within this run: what `compose` got.
    fresh: int = 0
    #: True when `post` ran and succeeded.
    posted: bool = False
    #: Names of the sources that raised or timed out.
    failed_sources: list[str] = field(default_factory=list)


def default_compose(fresh: Sequence[Any], key: Callable[[Any], str]) -> str:
    """The deterministic text posted when there is no `compose`: one line per
    item, "- title" and its url beneath. Items are dicts (or objects) with
    `title` and `url`; anything else falls back to its key."""

    def field_of(item: Any, name: str) -> Optional[str]:
        value = item.get(name) if isinstance(item, Mapping) else getattr(item, name, None)
        return value if isinstance(value, str) and value else None

    lines = []
    for item in fresh:
        title = field_of(item, "title") or str(key(item))
        url = field_of(item, "url")
        lines.append(f"- {title}\n  {url}" if url and url != title else f"- {title}")
    return "\n".join(lines)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _run_source(fn: Source, timeout: float) -> Sequence[Any]:
    async def call() -> Sequence[Any]:
        if inspect.iscoroutinefunction(fn):
            return await fn()
        # A plain function may block: keep it off the event loop.
        return await _maybe_await(await asyncio.to_thread(fn))

    return await asyncio.wait_for(call(), timeout)


async def run_script_job(
    *,
    sources: Union[Mapping[str, Source], Sequence[Source]],
    key: Callable[[Any], str],
    seen: SeenStore,
    post: Post,
    compose: Optional[Compose] = None,
    source_timeout_seconds: float = DEFAULT_SOURCE_TIMEOUT_SECONDS,
    seen_ttl_seconds: Optional[float] = None,
    logger: Optional[logging.Logger] = None,
) -> ScriptJobResult:
    """Runs one pass of a script job and says what happened. See the module
    docstring for the contract; the short version: fetch in code, drop what
    was sent, compose only if something is new, post, and only then mark seen.

    `sources` is a mapping of name to function (`{"hn": hacker_news}`) or a
    plain list; the name is what `failed_sources` reports (a list entry is
    named by its function name, else `source-<index>`). `compose` may return
    an empty string or None: nothing is posted and nothing is marked."""
    log = logger or _logger
    if isinstance(sources, Mapping):
        named = list(sources.items())
    else:
        named = [(getattr(fn, "__name__", "") or f"source-{i}", fn) for i, fn in enumerate(sources)]

    async def fetch(name: str, fn: Source) -> tuple[str, list[Any], bool]:
        try:
            items = await _run_source(fn, source_timeout_seconds)
            return name, list(items or []), True
        except asyncio.TimeoutError:
            log.error("[script job] source %s failed: timed out after %ss", name, source_timeout_seconds)
        except Exception as exc:  # noqa: BLE001 -- one source never stops the rest
            log.error("[script job] source %s failed: %s", name, exc)
        return name, [], False

    settled = await asyncio.gather(*(fetch(name, fn) for name, fn in named))

    result = ScriptJobResult(failed_sources=[name for name, _, ok in settled if not ok])
    every = [item for _, items, _ in settled for item in items]
    result.fetched = len(every)

    # Source order decides which copy of a duplicate wins.
    fresh: list[Any] = []
    fresh_keys: list[str] = []
    in_run: set[str] = set()
    for item in every:
        try:
            k = str(key(item))
        except Exception as exc:  # noqa: BLE001
            log.error("[script job] key() raised, item skipped: %s", exc)
            continue
        if k in in_run:
            continue
        in_run.add(k)
        if await seen.has(k):
            continue
        fresh.append(item)
        fresh_keys.append(k)

    result.fresh = len(fresh)
    if not fresh:
        return result

    text = await _maybe_await(compose(fresh)) if compose else default_compose(fresh, key)
    if not isinstance(text, str) or not text.strip():
        return result

    await _maybe_await(post(text, fresh))
    result.posted = True

    try:
        await seen.mark(fresh_keys, ttl_seconds=seen_ttl_seconds)
    except Exception as exc:  # noqa: BLE001
        # The post already went out; raising now would tell the scheduler it
        # did not. The cost is a possible repeat next run, so say so loudly.
        log.error("[script job] posted, but marking %d item(s) seen failed (they may repeat): %s", len(fresh_keys), exc)
    return result
