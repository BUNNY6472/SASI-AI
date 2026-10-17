import asyncio
import threading

import pytest

from app.files import FileRejected
from app.main import _prepare_upload


@pytest.mark.parametrize("cancel", [False, True])
def test_timed_out_or_cancelled_upload_holds_slot_until_parser_finishes(monkeypatch, cancel):
    async def scenario():
        slots = asyncio.Semaphore(1)
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        calls = []

        def blocked(*args):
            calls.append(args)
            loop.call_soon_threadsafe(started.set)
            release.wait(timeout=3)
            return "parsed"

        monkeypatch.setattr("app.main.files_mod.prepare", blocked)
        first = asyncio.create_task(_prepare_upload(slots, "first.txt", "text/plain", b"x", timeout_s=1 if cancel else 0.1))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            if cancel:
                first.cancel()
            with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
                await first
            assert slots.locked(), "the original parser is still running"
            with pytest.raises(TimeoutError):
                await _prepare_upload(slots, "second.txt", "text/plain", b"y", timeout_s=0.03)
            assert len(calls) == 1, "a second worker cannot start while the first owns the slot"
        finally:
            release.set()
            await asyncio.wait_for(slots.acquire(), timeout=1)
            slots.release()
        assert not slots.locked()
    asyncio.run(scenario())


def test_parser_error_releases_slot_and_propagates(monkeypatch):
    async def scenario():
        slots = asyncio.Semaphore(1)

        def rejected(*args):
            raise FileRejected("invalid file")

        monkeypatch.setattr("app.main.files_mod.prepare", rejected)
        with pytest.raises(FileRejected):
            await _prepare_upload(slots, "bad.txt", "text/plain", b"x", timeout_s=1)
        assert not slots.locked()
    asyncio.run(scenario())
