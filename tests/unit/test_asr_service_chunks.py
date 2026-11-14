"""How the Qwen engine groups pause-cut segments into the chunks it recognizes (asr_service/chunks.py)."""

from asr_service.chunks import Chunk, merge_turns, split_long

A, B = "发言人1", "发言人2"


def test_one_speaker_is_merged_up_to_the_limit():
    spans = [(0, 8000), (8500, 16000), (16200, 24000), (24500, 30000)]
    assert merge_turns(spans, [A] * 4, max_ms=25000) == [Chunk(0, 24000, A), Chunk(24500, 30000, A)]


def test_a_change_of_speaker_starts_a_chunk():
    spans = [(0, 3000), (3500, 6000), (6200, 9000), (9500, 12000)]
    assert merge_turns(spans, [A, A, B, A], max_ms=25000) == [
        Chunk(0, 6000, A),
        Chunk(6200, 9000, B),
        Chunk(9500, 12000, A),
    ]


def test_a_long_segment_is_a_chunk_of_its_own():
    spans = [(0, 2000), (2500, 30500), (31000, 33000)]
    assert merge_turns(spans, [A] * 3, max_ms=25000) == [
        Chunk(0, 2000, A),
        Chunk(2500, 30500, A),
        Chunk(31000, 33000, A),
    ]


def test_without_speaker_labels_only_the_length_counts():
    spans = [(0, 10000), (11000, 20000), (21000, 30000)]
    assert merge_turns(spans, [None] * 3, max_ms=25000) == [Chunk(0, 20000, None), Chunk(21000, 30000, None)]


def test_the_pause_inside_a_chunk_counts_toward_its_length():
    spans = [(0, 5000), (20000, 26000)]  # 15 s of silence between them
    assert merge_turns(spans, [A, A], max_ms=25000) == [Chunk(0, 5000, A), Chunk(20000, 26000, A)]


def test_no_segments():
    assert merge_turns([], [], max_ms=25000) == []


def quiet_at(ms_list, length_ms, frame_ms=100):
    """Loud everywhere except the frames holding the given times."""
    energy = [1.0] * (length_ms // frame_ms)
    for ms in ms_list:
        energy[ms // frame_ms] = 0.01
    return energy


def test_a_long_span_is_cut_where_it_is_quietest_before_the_limit():
    energy = quiet_at([22_000], 60_000)  # a pause 3 s before the 25 s limit
    assert split_long([(0, 40_000)], energy, frame_ms=100, max_ms=25_000, search_ms=5_000) == [
        (0, 22_050),
        (22_050, 40_000),
    ]


def test_it_keeps_cutting_until_every_piece_fits():
    energy = quiet_at([24_000, 47_000], 80_000)
    pieces = split_long([(0, 70_000)], energy, frame_ms=100, max_ms=25_000, search_ms=5_000)
    assert pieces == [(0, 24_050), (24_050, 47_050), (47_050, 70_000)]
    assert all(end - start <= 25_000 for start, end in pieces)


def test_a_quiet_spot_outside_the_search_window_is_not_used():
    energy = quiet_at([5_000], 60_000)  # too early: the cut must still come in the last 5 s
    [(start, cut), _] = split_long([(0, 30_000)], energy, frame_ms=100, max_ms=25_000, search_ms=5_000)
    assert 20_000 <= cut <= 25_000


def test_short_spans_are_left_alone():
    spans = [(0, 10_000), (12_000, 37_000)]  # exactly 25 s is short enough
    assert split_long(spans, [1.0] * 400, frame_ms=100, max_ms=25_000, search_ms=5_000) == spans
