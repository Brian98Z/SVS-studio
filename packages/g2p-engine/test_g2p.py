"""Consumer-visible regressions for the standalone G2P API."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from g2p import G2P, G2PError  # noqa: E402


class TestG2P(unittest.TestCase):
    def setUp(self):
        self.g = G2P()

    def test_mandarin_and_contextual_polyphones(self):
        self.assertEqual(self.g.convert("你好"), ["n", "i3", "h", "ao3"])
        self.assertEqual(self.g.convert("银行", language="zh"), ["y", "in2", "h", "ang2"])
        self.assertEqual(self.g.convert("重庆", language="mandarin"), ["ch", "ong2", "q", "ing4"])
    def test_mandarin_phrase_context_per_note(self):
        self.assertEqual(self.g.convert_per_note(["银", "行"], "zh"), [["y", "in2"], ["h", "ang2"]])
        self.assertEqual(self.g.convert_per_note(["行", "走"], "zh"), [["x", "ing2"], ["z", "ou3"]])


    def test_english_standard_g2p_and_unknown_word_error(self):
        self.assertEqual(self.g.convert("hello"), ["hh", "ah", "l", "ow"])
        with self.assertRaises(G2PError):
            self.g.convert("xyzzy")

    def test_japanese_kanji_kana_and_cantonese(self):
        self.assertTrue(self.g.convert("東京", language="ja"))
        self.assertEqual(self.g.convert("你好", language="yue"), ["n", "ei5", "h", "ou2"])

    def test_korean_selection_and_unknown_language(self):
        self.assertEqual(self.g.convert("안녕하세요", language="ko"), ["ㅇ", "ㅏ", "ㄴ", "ㄴ", "ㅕ", "ㅇ", "ㅎ", "ㅏ", "ㅅ", "ㅔ", "ㅇ", "ㅛ"])
        with self.assertRaises(G2PError):
            self.g.convert("hello", language="xx")

    def test_explicit_phonemes_take_precedence(self):
        self.assertEqual(self.g.convert("unreadable", language="xx", phonemes=["a", "b"]), ["a", "b"])

    def test_unknown_hanzi_fails_instead_of_inventing_vowel(self):
        with self.assertRaises(G2PError):
            self.g.convert("\ue000", language="zh")


if __name__ == "__main__":
    unittest.main(verbosity=2)
