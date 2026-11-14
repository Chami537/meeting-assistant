"""Several Fun-ASR model copies decoding segments at once (asr_service/parallel.py), with a thread pool and a fake
decoder in place of the worker processes and the model."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from asr_service.parallel import decode_all


def test_texts_come_back_in_segment_order():
    pieces = ["bb", "a", "dddd", "ccc", "", "eeeee"]
    with ThreadPoolExecutor(3) as pool:
        assert decode_all(pool, str.upper, pieces) == ["BB", "A", "DDDD", "CCC", "", "EEEEE"]


def test_longest_segments_are_started_first():
    started = []
    with ThreadPoolExecutor(1) as pool:  # one worker: the start order is the submission order
        decode_all(pool, lambda piece: started.append(piece) or piece, ["bb", "a", "dddd", "ccc"])
    assert started == ["dddd", "ccc", "bb", "a"]


def test_arguments_reach_every_call():
    with ThreadPoolExecutor(2) as pool:
        texts = decode_all(
            pool, lambda piece, kwargs: f"{piece}:{kwargs['language']}", ["x", "yy"], {"language": "中文"}
        )
    assert texts == ["x:中文", "yy:中文"]


def test_a_failure_cancels_the_segments_not_started():
    started, release = [], threading.Event()

    def decode(piece):
        started.append(piece)
        if piece.startswith("boom"):
            raise RuntimeError("CUDA out of memory")
        if piece == "block-long":  # keeps the only worker busy while the failure is handled
            assert release.wait(5)
        return piece

    with ThreadPoolExecutor(1) as pool:
        with pytest.raises(RuntimeError):
            decode_all(pool, decode, ["boom-longest-piece", "block-long", "a", "b"])
        release.set()
    assert "a" not in started and "b" not in started


def test_no_segments():
    with ThreadPoolExecutor(1) as pool:
        assert decode_all(pool, str.upper, []) == []
