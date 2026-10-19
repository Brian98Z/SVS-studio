"""Unit tests for the JSON bridge; run with python -m unittest discover -s packages/interchange."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import unittest
from bridge import dispatch

class BridgeTests(unittest.TestCase):
    def test_musicxml_roundtrip_keeps_offsets_tempo_meter_lyrics_ties_and_rests(self):
        source = {"schema": 1, "name": "roundtrip", "ppq": 480, "sample_rate": 24000,
                  "tempos": [{"tick": 0, "bpm": 90.0}],
                  "time_signatures": [{"tick": 0, "numerator": 3, "denominator": 4}],
                  "tracks": [{"id": "lead", "kind": "sing", "name": "Lead", "singer_id": "builtin.fallback",
                              "instrument": "", "volume": 1, "pan": 0, "mute": False, "solo": False, "colour": "",
                              "notes": [{"id": "n1", "tick": 480, "duration": 960, "key": 60, "velocity": .8,
                                         "lyric": "晨", "phonemes": [], "legato": False, "head_ticks": 0, "tail_ticks": 0}], "curves": {}}], "audio_clips": []}
        xml = dispatch({"action": "export_musicxml", "project": source})["content"]
        result = dispatch({"action": "import_musicxml", "content": xml, "project": {"ppq": 480}})["project"]
        note = result["tracks"][0]["notes"][0]
        self.assertEqual((note["tick"], note["duration"], note["key"], note["lyric"]), (480, 960, 60, "晨"))
        self.assertEqual(result["tempos"][0]["bpm"], 90)
        self.assertEqual((result["time_signatures"][0]["numerator"], result["time_signatures"][0]["denominator"]), (3, 4))

    def test_midi_roundtrip_multitrack_and_tempo(self):
        project = {"ppq": 480, "tempos": [{"tick": 0, "bpm": 120}], "time_signatures": [{"tick": 0, "numerator": 4, "denominator": 4}], "tracks": [
            {"name": "Lead", "notes": [{"tick": 240, "duration": 480, "key": 64, "velocity": .8}]},
            {"name": "Accompaniment", "notes": [{"tick": 0, "duration": 960, "key": 48, "velocity": .6}]}]}
        binary = dispatch({"action": "export_midi", "project": project})["content"]
        imported = dispatch({"action": "import_midi", "content": binary, "project": {"ppq": 480}})["project"]
        self.assertEqual(len(imported["tracks"]), 2)
        self.assertEqual(imported["tracks"][0]["notes"][0]["tick"], 240)
        self.assertEqual(imported["tempos"][0]["bpm"], 120)

    def test_lrc_uses_score_tempo_map(self):
        project = {"ppq": 480, "tempos": [{"tick": 0, "bpm": 120}], "tracks": [{"notes": [
            {"tick": 960, "duration": 480, "key": 60, "lyric": "远"}]}]}
        self.assertEqual(dispatch({"action": "export_lrc", "project": project})["content"], "[00:01.00]远\n")

    def test_midi_combines_all_accompaniment_tracks(self):
        project = {"ppq": 480, "tracks": [{"name": str(i), "kind": "sing" if i == 0 else "instrument", "notes": [{"tick": i * 120, "duration": 120, "key": 60 + i}]} for i in range(6)]}
        binary = dispatch({"action": "export_midi", "project": project})["content"]
        imported = dispatch({"action": "import_midi", "content": binary, "project": {"ppq": 480}})["project"]
        self.assertEqual(len(imported["tracks"]), 2)
        self.assertEqual(sum(len(t["notes"]) for t in imported["tracks"]), 6)
        self.assertTrue(dispatch({"action": "export_midi", "project": project})["warnings"])
    def test_ufdata_fixture_import(self):
        data = Path(__file__).with_name("fixtures").joinpath("ufdata-minimal.json").read_text(encoding="utf-8")
        result = dispatch({"action": "import_ufdata", "content": data, "project": {"ppq": 480}})["project"]
        self.assertEqual(result["tracks"][0]["notes"][0]["tick"], 240)
        self.assertEqual(result["tracks"][0]["notes"][0]["lyric"], "你")

    def test_musicxml_fixture_rest_and_tie(self):
        xml = Path(__file__).with_name("fixtures").joinpath("score.musicxml").read_text(encoding="utf-8")
        result = dispatch({"action": "import_musicxml", "content": xml, "project": {"ppq": 480}})["project"]
        notes = result["tracks"][0]["notes"]
        self.assertEqual([(n["tick"], n["duration"]) for n in notes], [(480, 480), (960, 960)])

if __name__ == "__main__": unittest.main()
