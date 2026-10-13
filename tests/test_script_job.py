# run_script_job: deterministic-first scheduled work. Pins the contract the
# module docstring promises -- each source once with a timeout, one failing
# source never stops the others, nothing new means no compose and no post,
# items are marked seen only after post succeeds -- and both SeenStore
# implementations (per-key TTL, atomic file). Mirrors salt-agent-sdk's
# tests/script-job.test.js.
from __future__ import annotations

import asyncio
import json
import os

import pytest

from saltapp import FileSeenStore, MemorySeenStore, run_script_job
from saltapp.script_job import ScriptJobResult, default_compose


def item(url, title=None):
    return {"url": url, "title": title or url}


def by_url(i):
    return i["url"]


class Recorder:
    def __init__(self):
        self.compose_calls = []
        self.post_calls = []

    async def compose(self, fresh):
        self.compose_calls.append(fresh)
        return f"digest of {len(fresh)}"

    async def post(self, text, fresh):
        self.post_calls.append((text, fresh))


async def test_fetches_composes_only_the_new_items_posts_and_reports_the_counts():
    seen = MemorySeenStore()
    await seen.mark(["https://old"])
    rec = Recorder()

    async def hn():
        return [item("https://a"), item("https://old")]

    async def rss():
        return [item("https://b")]

    result = await run_script_job(sources={"hn": hn, "rss": rss}, key=by_url, seen=seen, compose=rec.compose, post=rec.post)
    assert result == ScriptJobResult(fetched=3, fresh=2, posted=True, failed_sources=[])
    assert len(rec.compose_calls) == 1
    assert [by_url(i) for i in rec.compose_calls[0]] == ["https://a", "https://b"]
    assert rec.post_calls[0][0] == "digest of 2"


async def test_nothing_new_means_no_compose_and_no_post():
    seen = MemorySeenStore()
    await seen.mark(["https://a"])
    rec = Recorder()

    async def src():
        return [item("https://a")]

    result = await run_script_job(sources=[src], key=by_url, seen=seen, compose=rec.compose, post=rec.post)
    assert result == ScriptJobResult(fetched=1, fresh=0, posted=False, failed_sources=[])
    assert rec.compose_calls == [] and rec.post_calls == []


async def test_a_source_returning_nothing_is_silent():
    rec = Recorder()

    async def empty():
        return []

    result = await run_script_job(sources={"empty": empty}, key=by_url, seen=MemorySeenStore(), compose=rec.compose, post=rec.post)
    assert result == ScriptJobResult(fetched=0, fresh=0, posted=False, failed_sources=[])
    assert rec.post_calls == []


async def test_one_failing_source_never_stops_the_others_and_is_named(caplog):
    rec = Recorder()

    async def broken():
        raise RuntimeError("503 from the feed")

    async def working():
        return [item("https://ok")]

    result = await run_script_job(sources={"broken": broken, "working": working}, key=by_url, seen=MemorySeenStore(), compose=rec.compose, post=rec.post)
    assert result == ScriptJobResult(fetched=1, fresh=1, posted=True, failed_sources=["broken"])
    assert any("broken failed: 503 from the feed" in r.getMessage() for r in caplog.records)


async def test_a_source_that_hangs_is_cut_off_by_its_timeout_while_the_others_finish():
    async def hangs():
        await asyncio.sleep(30)
        return []

    async def fast():
        return [item("https://x")]

    async def post(text, fresh):
        pass

    result = await run_script_job(sources={"hangs": hangs, "fast": fast}, key=by_url, seen=MemorySeenStore(), post=post, source_timeout_seconds=0.05)
    assert result.failed_sources == ["hangs"]
    assert result.posted is True


async def test_each_source_runs_exactly_once_and_sync_sources_work():
    runs = {"async": 0, "sync": 0}

    async def counted():
        runs["async"] += 1
        return [item("https://a")]

    def plain():
        runs["sync"] += 1
        return [item("https://b")]

    async def post(text, fresh):
        pass

    result = await run_script_job(sources=[counted, plain], key=by_url, seen=MemorySeenStore(), post=post)
    assert runs == {"async": 1, "sync": 1}
    assert result.fresh == 2


async def test_items_are_marked_seen_only_after_post_succeeds():
    seen = MemorySeenStore()

    async def src():
        return [item("https://a")]

    async def failing_post(text, fresh):
        raise RuntimeError("chat unreachable")

    with pytest.raises(RuntimeError, match="chat unreachable"):
        await run_script_job(sources={"s": src}, key=by_url, seen=seen, post=failing_post)
    assert await seen.has("https://a") is False

    rec = Recorder()
    second = await run_script_job(sources={"s": src}, key=by_url, seen=seen, post=rec.post)
    assert second.posted is True
    assert await seen.has("https://a") is True

    third = await run_script_job(sources={"s": src}, key=by_url, seen=seen, post=rec.post)
    assert third == ScriptJobResult(fetched=1, fresh=0, posted=False, failed_sources=[])


async def test_a_compose_that_raises_posts_nothing_and_marks_nothing():
    seen = MemorySeenStore()
    posted = []

    async def src():
        return [item("https://a")]

    async def compose(fresh):
        raise RuntimeError("model down")

    async def post(text, fresh):
        posted.append(text)

    with pytest.raises(RuntimeError, match="model down"):
        await run_script_job(sources=[src], key=by_url, seen=seen, compose=compose, post=post)
    assert posted == []
    assert await seen.has("https://a") is False


@pytest.mark.parametrize("returned", ["   ", "", None])
async def test_compose_returning_nothing_posts_nothing_and_marks_nothing(returned):
    seen = MemorySeenStore()
    posted = []

    async def src():
        return [item("https://a")]

    def compose(fresh):  # a sync compose is fine too
        return returned

    async def post(text, fresh):
        posted.append(text)

    result = await run_script_job(sources=[src], key=by_url, seen=seen, compose=compose, post=post)
    assert result.posted is False and posted == []
    assert await seen.has("https://a") is False


async def test_without_compose_a_plain_list_is_posted_and_a_story_from_two_sources_counts_once():
    posted = []

    async def hn():
        return [item("https://a", "Story A")]

    async def rss():
        return [item("https://a", "Story A again"), {"url": "https://b"}]

    async def post(text, fresh):
        posted.append(text)

    result = await run_script_job(sources={"hn": hn, "rss": rss}, key=by_url, seen=MemorySeenStore(), post=post)
    assert (result.fetched, result.fresh) == (3, 2)
    assert posted == ["- Story A\n  https://a\n- https://b"]


async def test_an_item_whose_key_raises_is_skipped_not_fatal():
    async def src():
        return [item("https://a"), None]

    async def post(text, fresh):
        pass

    result = await run_script_job(sources=[src], key=lambda i: i["url"], seen=MemorySeenStore(), post=post)
    assert result == ScriptJobResult(fetched=2, fresh=1, posted=True, failed_sources=[])


async def test_seen_ttl_seconds_reaches_the_store_so_posted_items_can_come_back_later():
    now = [1000.0]
    seen = MemorySeenStore(now=lambda: now[0])

    async def src():
        return [item("https://a")]

    async def post(text, fresh):
        pass

    kwargs = dict(sources=[src], key=by_url, seen=seen, post=post)
    await run_script_job(**kwargs, seen_ttl_seconds=100)
    assert (await run_script_job(**kwargs)).posted is False
    now[0] += 101
    assert (await run_script_job(**kwargs)).posted is True


def test_default_compose_accepts_objects_and_falls_back_to_the_key():
    class Obj:
        title = "Hi"
        url = "https://x"

    assert default_compose([Obj(), 5], lambda i: "k5" if i == 5 else "kobj") == "- Hi\n  https://x\n- k5"


async def test_memory_seen_store_per_key_ttl_overrides_default_inf_never_expires_and_forget_works():
    now = [0.0]
    store = MemorySeenStore(ttl_seconds=1000, now=lambda: now[0])
    await store.mark(["default"])
    await store.mark(["short"], ttl_seconds=10)
    await store.mark(["forever"], ttl_seconds=float("inf"))
    now[0] = 11
    assert await store.has("short") is False
    assert await store.has("default") is True
    now[0] = 5000
    assert await store.has("default") is False
    assert await store.has("forever") is True
    await store.forget("forever")
    assert await store.has("forever") is False
    await store.forget("never-marked")


async def test_file_seen_store_survives_a_new_instance_honours_ttl_prunes_on_write_and_writes_atomically(tmp_path):
    path = str(tmp_path / "nested" / "seen.json")
    now = [1_000_000.0]
    a = FileSeenStore(path, ttl_seconds=60, now=lambda: now[0])
    assert await a.has("x") is False  # a missing file is an empty store
    await a.mark(["x", "y"])
    await a.mark(["short"], ttl_seconds=10)

    b = FileSeenStore(path, now=lambda: now[0])
    assert await b.has("x") is True

    now[0] += 11
    assert await b.has("short") is False
    assert await b.has("y") is True

    await b.mark(["z"])
    on_disk = json.loads(open(path).read())
    assert "short" not in on_disk
    assert sorted(on_disk) == ["x", "y", "z"]
    assert os.listdir(os.path.dirname(path)) == ["seen.json"]

    await b.forget("x")
    assert await FileSeenStore(path, now=lambda: now[0]).has("x") is False


async def test_file_seen_store_overlapping_marks_do_not_lose_keys(tmp_path):
    store = FileSeenStore(str(tmp_path / "seen.json"))
    await asyncio.gather(*(store.mark([f"k{i}"]) for i in range(20)))
    for i in range(20):
        assert await store.has(f"k{i}") is True


async def test_file_seen_store_a_corrupt_file_raises_rather_than_resending_everything(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text("{not json")
    with pytest.raises(ValueError):
        await FileSeenStore(str(path)).has("x")
