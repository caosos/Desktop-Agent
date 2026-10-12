"""SIGTERM releases the open event streams before uvicorn waits for connections, so stop is clean."""
import asyncio
import signal

from desktop_agent.__main__ import make_server
from desktop_agent.control.api import build_app
from tests.test_ask_aria import _service


def test_exit_signal_releases_streams_then_defers_to_uvicorn(tmp_path):
    s = _service(tmp_path)
    server = make_server(build_app(s, "tok"), s, "127.0.0.1", 0)
    async def run():
        q = s.subscribe()                              # what an open /v0/events stream holds
        assert not s.stopping and not server.should_exit
        server.handle_exit(signal.SIGTERM, None)
        assert s.stopping and q.get_nowait() is None   # the stream's wait is woken with the shutdown sentinel
        assert server.should_exit                      # uvicorn's own handler still ran
        server.handle_exit(signal.SIGTERM, None)       # a second signal is harmless (uvicorn escalates to force exit)
    asyncio.run(run())
