"""Focused media interchange behavior tests."""
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inference_service import media_io as media  # noqa: E402


def _tools():
    if not media.shutil.which("ffmpeg") or not media.shutil.which("ffprobe"):
        pytest.skip("FFmpeg tools are not on PATH")


def _audio(path):
    _tools()
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-ac", "2", "-y", str(path)], check=True)


def test_ticks_to_seconds_integrates_tempo_changes():
    tempos = [{"tick": 0, "bpm": 120}, {"tick": 960, "bpm": 60}]
    assert media.ticks_to_seconds(480, tempos, 480) == 0.5
    assert media.ticks_to_seconds(1440, tempos, 480) == 2.0


def test_ticks_to_seconds_rejects_bad_tempo_map():
    with pytest.raises(media.MediaError): media.ticks_to_seconds(10, [{"tick": 0, "bpm": 0}], 480)


def test_probe_and_decode_real_audio(tmp_path):
    path=tmp_path/"source.wav"; _audio(path)
    info=media.probe_media(str(path)); decoded=media.decode_audio(str(path),24000)
    assert info["audio"] and decoded["channels"]==2 and decoded["frames"]==24000


def test_fcpxml_is_well_formed_and_contains_aligned_asset(tmp_path):
    source=tmp_path/"source.wav"; _audio(source)
    dest=tmp_path/"project.fcpxml"
    media.export_fcpxml({"name":"Test","ppq":480,"tempos":[{"tick":0,"bpm":120}],"audio_clips":[{"path":str(source),"start_tick":480}]},str(dest))
    root=ET.parse(dest).getroot()
    assert root.tag=="fcpxml" and root.find(".//asset-clip").attrib["offset"]=="1/2s"


def test_aaf_roundtrip_contains_composition_and_audio_essence(tmp_path):
    _tools(); source=tmp_path/"source.wav"; _audio(source)
    dest=tmp_path/"project.aaf"
    media.export_aaf({"name":"Test","ppq":480,"tempos":[{"tick":0,"bpm":120}],"audio_clips":[
        {"path":str(source),"track_id":"a","start_tick":480},
        {"path":str(source),"track_id":"b","start_tick":480},
    ]},str(dest))
    import aaf2
    with aaf2.open(str(dest),"r") as f:
        composition=next(f.content.compositionmobs())
        assert composition.name=="Test"
        assert len(composition.slots)==2
        assert all(slot.segment.length>=72000 for slot in composition.slots)
        assert len(list(f.content.mastermobs()))==2
def test_video_roundtrip_preserves_video_and_audio_streams(tmp_path):
    _tools()
    video=tmp_path/"video.mp4"; audio=tmp_path/"audio.wav"; output=tmp_path/"out.mp4"
    subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","color=c=blue:s=64x64:d=1","-c:v","libx264","-pix_fmt","yuv420p","-y",str(video)],check=True)
    _audio(audio); media.assemble_video(str(video),str(audio),str(output))
    streams=media._probe(output)["streams"]
    assert any(s["codec_type"]=="video" for s in streams)
    assert any(s["codec_type"]=="audio" for s in streams)


def test_output_refuses_overwrite_and_unsupported_media(tmp_path):
    existing=tmp_path/"already.fcpxml"; existing.write_text("user data")
    with pytest.raises(media.MediaError,match="overwrite"): media._new_output(str(existing),{".fcpxml"})
    bad=tmp_path/"bad.mid"; bad.write_bytes(b"no")
    with pytest.raises(media.MediaError,match="Unsupported"): media.probe_media(str(bad))
