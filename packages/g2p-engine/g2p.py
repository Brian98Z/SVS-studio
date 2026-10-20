"""Language-aware grapheme-to-phoneme conversion for SVS lyrics."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

DICTS = Path(__file__).resolve().parent / "dicts"


class G2PError(ValueError):
    """Input cannot be converted without guessing a pronunciation."""


def _load(path: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) == 2:
                result[parts[0]] = parts[1].split()
    return result


class G2P:
    def __init__(self, dicts_dir: Path = DICTS):
        self.zh = _load(dicts_dir / "mandarin.tsv")
        self.en = _load(dicts_dir / "cmu_fragment.tsv")
        self._english = None
        self._kakasi = None
        self._korean = None

    def _zh_convert(self, text: str) -> list[str]:
        from pypinyin import Style, pinyin
        rows = pinyin(text, style=Style.TONE3, heteronym=False, errors=lambda chars: list(chars))
        out: list[str] = []
        for row in rows:
            syllable = row[0]
            if not re.fullmatch(r"[a-züv]+[1-5]", syllable):
                raise G2PError(f"No Mandarin pronunciation for {syllable!r}")
            tone = syllable[-1]
            base = syllable[:-1].replace("ü", "v").replace("ü", "v")
            onset = next((x for x in ("zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l", "g", "k", "h", "j", "q", "x", "r", "z", "c", "s", "y", "w") if base.startswith(x)), "")
            rime = base[len(onset):]
            if onset:
                out.append(onset)
            if rime:
                out.append(rime + tone)
            elif not onset:
                raise G2PError(f"No Mandarin pronunciation for {syllable!r}")
        return out

    def _en_convert(self, word: str) -> list[str]:
        word = word.lower()
        if word in self.en:
            return self.en[word]
        try:
            import cmudict
            if word not in cmudict.dict():
                raise G2PError(f"No English pronunciation for {word!r}")
            from g2p_en import G2p
        except ImportError as exc:
            raise G2PError("English conversion requires g2p-en and cmudict; install from repository-root requirements-g2p.txt") from exc
        if self._english is None:
            self._english = G2p()
        return [p.lower() for p in self._english(word) if p.strip() and p != " "]

    def _kana_convert(self, text: str) -> list[str]:
        try:
            import pykakasi
        except ImportError as exc:
            raise G2PError("Japanese conversion requires pykakasi; install from repository-root requirements-g2p.txt") from exc
        if self._kakasi is None:
            self._kakasi = pykakasi.kakasi()
        romaji = "".join(item["hepburn"] for item in self._kakasi.convert(text))
        phones = {"a":"a","i":"i","u":"u","e":"e","o":"o","n":"n","k":"k","g":"g","s":"s","z":"z","t":"t","d":"d","h":"h","b":"b","p":"p","m":"m","y":"y","r":"r","w":"w","f":"f","j":"j","c":"ch","sh":"sh","ch":"ch","ts":"ts","ky":"ky","ry":"ry","ny":"ny","sy":"sh","ty":"ch","gy":"gy","by":"by","py":"py","my":"my","hy":"hy","dy":"j","zy":"j"}
        out: list[str] = []
        i = 0
        while i < len(romaji):
            unit = next((x for x in ("ky","ry","ny","sy","ty","gy","by","py","my","hy","dy","zy","sh","ch","ts") if romaji.startswith(x, i)), None)
            if unit:
                out.append(phones[unit]); i += len(unit); continue
            if romaji[i] not in phones:
                raise G2PError(f"Unsupported Japanese romanization {romaji[i]!r}")
            out.append(phones[romaji[i]]); i += 1
        return out

    @staticmethod
    def _jyutping_to_phones(text: str) -> list[str]:
        try:
            from pycantonese import characters_to_jyutping
        except ImportError as exc:
            raise G2PError("Cantonese conversion requires pycantonese; install from repository-root requirements-g2p.txt") from exc
        initials = ("gw", "kw", "ng", "b", "p", "m", "f", "d", "t", "n", "l", "g", "k", "h", "w", "z", "c", "s", "j")
        out: list[str] = []
        for word, syllables in characters_to_jyutping(text):
            values = (syllables,) if isinstance(syllables, str) else syllables
            if values is None or any(syllable is None for syllable in values):
                raise G2PError(f"No Cantonese pronunciation for {word!r}")
            for syllable in values:
                match = re.fullmatch(r"([a-z]+)([1-6])", syllable)
                if not match:
                    raise G2PError(f"Invalid Jyutping syllable {syllable!r}")
                base, tone = match.groups()
                onset = next((initial for initial in initials if base.startswith(initial)), "")
                rime = base[len(onset):]
                if onset:
                    out.append(onset)
                if rime:
                    out.append(rime + tone)
        return out
    def _ko_convert(self, text: str) -> list[str]:
        try:
            from g2pk import G2p as KoreanG2p
            from jamo import h2j
        except ImportError as exc:
            raise G2PError("Korean conversion requires g2pk and jamo; install from repository-root requirements-g2p.txt") from exc
        if self._korean is None:
            self._korean = KoreanG2p()
        normalized = self._korean(text)
        # g2pk applies Korean phonological rules; decompose normalized Hangul into jamo.
        compatibility = dict(zip("ᄀᄁᄂᄃᄄᄅᄆᄇᄈᄉᄊᄋᄌᄍᄎᄏᄐᄑ하ᅢᅣᅤᅥᅦᅧᅨᅩᅪᅫᅬᅭᅮᅯᅰᅱᅲᅳᅴᅵᅪᅫᅬᆨᆩᆪᆫᆬᆭᆮᆯᆰᆱᆲᆳᆴᆵᆶᆷᆸᆹᆺᆻᆼᆽᆾᆿᇀᇁᇂ", "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣㅘㅙㅚㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"))
        return [compatibility.get(unit, unicodedata.normalize("NFKC", unit)) for unit in h2j(normalized)]

    @staticmethod
    def _script_language(text: str) -> str | None:
        for ch in text:
            code = ord(ch)
            if 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF:
                return "ko"
            if 0x3040 <= code <= 0x30FF:
                return "ja"
            if 0x3400 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF:
                return "zh"
            if ch.isascii() and ch.isalpha():
                return "en"
        return None

    def _convert_language(self, text: str, language: str) -> list[str]:
        lang = language.lower().replace("_", "-")
        if lang in ("zh", "zh-cn", "mandarin", "cmn"):
            return self._zh_convert(text)
        if lang in ("yue", "zh-yue", "cantonese", "粤语", "廣東話"):
            return self._jyutping_to_phones(text)
        if lang in ("en", "en-us", "english"):
            out: list[str] = []
            for word in re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text):
                out.extend(self._en_convert(word))
            return out
        if lang in ("ja", "ja-jp", "japanese"):
            return self._kana_convert(text)
        if lang in ("ko", "ko-kr", "korean", "한국어"):
            return self._ko_convert(text)
        raise G2PError(f"Unsupported language: {language}")

    def convert_per_note(self, syllables: list[str], language: str) -> list[list[str]]:
        """Convert note lyric units while preserving phrase context for Mandarin."""
        lang = language.lower().replace("_", "-")
        if lang in ("zh", "zh-cn", "mandarin", "cmn"):
            from pypinyin import Style, pinyin
            phrase = "".join(syllables)
            readings = pinyin(phrase, style=Style.TONE3, heteronym=False, errors=lambda chars: list(chars))
            if len(readings) != len(phrase):
                raise G2PError("Mandarin per-note alignment could not be established")
            result: list[list[str]] = []
            cursor = 0
            for lyric in syllables:
                result.append(self._pinyin_syllables_to_phones([row[0] for row in readings[cursor:cursor + len(lyric)]]))
                cursor += len(lyric)
            return result
        return [self._convert_language(lyric, language) for lyric in syllables]

    @staticmethod
    def _pinyin_syllables_to_phones(syllables: list[str]) -> list[str]:
        out: list[str] = []
        for syllable in syllables:
            match = re.fullmatch(r"([a-züv]+)([1-5])", syllable)
            if not match:
                raise G2PError(f"No Mandarin pronunciation for {syllable!r}")
            base, tone = match.groups()
            base = base.replace("ü", "v").replace("ü", "v")
            onset = next((x for x in ("zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l", "g", "k", "h", "j", "q", "x", "r", "z", "c", "s", "y", "w") if base.startswith(x)), "")
            rime = base[len(onset):]
            if onset:
                out.append(onset)
            if rime:
                out.append(rime + tone)
        return out

    def convert(self, text: str, language: str | None = None, phonemes: list[str] | None = None) -> list[str]:
        if phonemes is not None:
            return list(phonemes)
        text = unicodedata.normalize("NFKC", text).strip()
        if language:
            return self._convert_language(text, language)
        output: list[str] = []
        for token in re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?|[\u3040-\u30ff]+|[\u3400-\u9fff\uf900-\ufaff]+|[\uac00-\ud7af\u1100-\u11ff]+", text):
            lang = self._script_language(token)
            if lang:
                output.extend(self._convert_language(token, lang))
        return output

    def convert_per_char(self, text: str, language: str | None = None) -> list[tuple[str, list[str]]]:
        if language:
            return [(text, self.convert(text, language=language))]
        return [(token, self.convert(token)) for token in re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?|[\u3040-\u30ff]+|[\u3400-\u9fff\uf900-\ufaff]+|[\uac00-\ud7af\u1100-\u11ff]+", text)]
