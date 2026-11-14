"""ffprobe output handling: only ordinary containers are accepted (playlists could make ffmpeg read other
files), durations and loudness are parsed defensively."""

import json

import pytest
from app.postprocess import media


def probe_json(format_name, duration="61.5", streams=("audio",)):
    return json.dumps(
        {
            "format": {"format_name": format_name, "duration": duration},
            "streams": [{"codec_type": t} for t in streams],
        }
    ).encode()


@pytest.mark.parametrize(
    ("format_name", "demuxer"),
    [("mov,mp4,m4a,3gp,3g2,mj2", "mov"), ("mp3", "mp3"), ("wav", "wav"), ("matroska,webm", "matroska")],
)
def test_accepted_containers(format_name, demuxer):
    info = media.parse_probe(probe_json(format_name))
    assert info == media.MediaInfo(demuxer=demuxer, duration_ms=61_500, has_audio=True)


@pytest.mark.parametrize("format_name", ["hls", "concat", "image2", "png_pipe", "tty", "lavfi"])
def test_playlists_and_non_media_are_refused(format_name):
    assert media.parse_probe(probe_json(format_name)) is None


def test_video_without_audio_is_detected():
    info = media.parse_probe(probe_json("mov,mp4", streams=("video",)))
    assert info is not None and info.has_audio is False


@pytest.mark.parametrize("raw", [b"", b"not json", b"{}", json.dumps({"format": {}}).encode()])
def test_garbage_probe_output(raw):
    assert media.parse_probe(raw) is None


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (b"[Parsed_volumedetect_0 @ 0x1] mean_volume: -23.4 dB\n", -23.4),
        (b"mean_volume: -inf dB", media.SILENCE_FLOOR_DBFS),
        (b"mean_volume: -1200.0 dB", media.SILENCE_FLOOR_DBFS),
        (b"nothing here", None),
    ],
)
def test_mean_volume(stderr, expected):
    assert media.parse_mean_volume(stderr) == expected


async def test_unaccepted_demuxer_is_never_passed_to_ffmpeg(tmp_path):
    with pytest.raises(media.MediaRejected):
        await media.Ffmpeg(ffmpeg="/bin/false").to_speech_audio(tmp_path / "a", tmp_path / "b", demuxer="hls")


async def test_tool_timeout(monkeypatch):
    monkeypatch.setattr(media, "PROBE_TIMEOUT_S", 0.05)
    with pytest.raises(media.MediaRejected):
        await media._run(["sleep", "5"], timeout=0.05)


async def test_cancelled_tool_is_killed():
    import asyncio

    task = asyncio.create_task(media._run(["sleep", "30"], timeout=60))
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    proc = await asyncio.create_subprocess_exec("pgrep", "-f", "^sleep 30$", stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    assert out.strip() == b""  # no orphaned child left running
