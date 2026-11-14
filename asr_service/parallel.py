"""Decoding segments on several model copies at once. Free of torch and funasr, so it is tested locally."""

from collections.abc import Callable, Sequence
from concurrent.futures import Executor
from typing import Any


def decode_all(pool: Executor, decode: Callable[..., str], pieces: Sequence[Sequence], *args: Any) -> list:
    """Longest piece first, each worker taking the next one when it is free (no copy is left alone with the
    longest at the end); texts come back in the pieces' order. On a failure the pieces not started are
    cancelled and the error is raised."""
    order = sorted(range(len(pieces)), key=lambda i: -len(pieces[i]))
    futures = {i: pool.submit(decode, pieces[i], *args) for i in order}
    try:
        return [futures[i].result() for i in range(len(pieces))]
    except BaseException:
        for future in futures.values():
            future.cancel()
        raise
