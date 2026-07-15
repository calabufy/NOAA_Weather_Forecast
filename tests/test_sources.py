from contextlib import nullcontext

from tenacity import wait_none

from app import sources


def test_stream_retry_covers_failure_during_iteration(monkeypatch):
    attempts = {"n": 0}

    class Response:
        def raise_for_status(self):
            return None

        def iter_lines(self):
            attempts["n"] += 1
            if attempts["n"] == 1:
                yield "first"
                raise RuntimeError("connection dropped mid-stream")
            yield "complete"

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, *_args):
            return nullcontext(Response())

    monkeypatch.setattr(sources, "_client", Client)
    no_wait = sources.http_stream_extract.retry_with(wait=wait_none())

    result = no_wait("https://example.test/bulk", lambda lines: "\n".join(lines))

    assert result == "complete"
    assert attempts["n"] == 2
