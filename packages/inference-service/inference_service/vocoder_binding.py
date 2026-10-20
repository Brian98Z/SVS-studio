"""Validation and assembly for user-owned external DiffSinger vocoders."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_MATCH_FIELDS = ("name", "sample_rate", "hop_size", "win_size", "fft_size", "num_mel_bins",
                 "mel_fmin", "mel_fmax", "mel_base", "mel_scale")


def _yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid YAML mapping: {path.name}")
    return data


def inspect_vocoder(acoustic_root: Path, external_path: str | Path | None = None) -> dict[str, Any]:
    """Describe configured bundled/external vocoder and validate authoritative model refs."""
    acoustic_root = acoustic_root.resolve()
    source = "external" if external_path else "bundled"
    root = Path(external_path).expanduser().resolve() if external_path else acoustic_root / "dsvocoder"
    config = root / "vocoder.yaml"
    missing: list[str] = []
    issues: list[str] = []
    if not config.is_file():
        missing.append(str(config))
        return {"source": source if external_path else "missing", "path": str(root), "compatible": False,
                "missing_files": missing, "issues": issues}
    if not config.resolve().is_relative_to(root):
        return {"source": source, "path": str(root), "compatible": False, "missing_files": [],
                "issues": ["vocoder.yaml escapes vocoder directory"]}
    try:
        voc = _yaml(config)
        acoustic = _yaml(acoustic_root / "dsconfig.yaml")
    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
        return {"source": source, "path": str(root), "compatible": False, "missing_files": missing,
                "issues": [f"Invalid configuration: {exc}"]}
    ref = voc.get("model")
    model = (root / ref).resolve() if isinstance(ref, str) and ref else None
    confined = model is not None and model.is_relative_to(root)
    if not confined:
        issues.append("model reference escapes vocoder directory or is invalid")
    elif not model.is_file():
        missing.append(str(model))
    expected = dict(acoustic)
    expected["name"] = acoustic.get("vocoder")
    mismatches = [key for key in _MATCH_FIELDS if key not in voc or key not in expected
                  or isinstance(voc[key], bool) or isinstance(expected[key], bool)
                  or voc[key] != expected[key]]
    if mismatches:
        issues.append("configuration mismatch: " + ", ".join(mismatches))
    return {"source": source, "path": str(root), "compatible": not missing and not issues,
            "missing_files": missing, "issues": issues}


def validate_binding(acoustic_root: Path, external_path: str | Path) -> Path:
    """Raise before registry writes unless an external vocoder is complete and compatible."""
    root = Path(external_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Vocoder directory does not exist or is not a directory")
    result = inspect_vocoder(acoustic_root, root)
    if not result["compatible"]:
        raise ValueError("Incompatible vocoder: " + "; ".join(result["issues"] + result["missing_files"]))
    return root


def build_predictor(voicebank: Path, vocoder_path: str | Path | None):
    """Compose upstream supported readers and predictors without PredAll's hardcoded vocoder."""
    from diffsinger_utau.voice_bank.commons.voice_bank_reader import VoiceBankReader
    from diffsinger_utau.voice_bank.pred_duration import PredDuration
    from diffsinger_utau.voice_bank.pred_pitch import PredPitch
    from diffsinger_utau.voice_bank.pred_variance import PredVariance
    from diffsinger_utau.voice_bank.pred_acoustic import PredAcoustic
    from diffsinger_utau.voice_bank.pred_vocoder import PredVocoder

    bank = voicebank.resolve()
    reader = VoiceBankReader.__new__(VoiceBankReader)
    reader.voice_bank_path = bank
    reader.preload_models = False
    reader.dsdur = VoiceBankReader.DSDur(bank / "dsdur" / "dsconfig.yaml", False)
    reader.dspitch = VoiceBankReader.DSPitch(bank / "dspitch" / "dsconfig.yaml", False)
    reader.dsvariance = VoiceBankReader.DSVariance(bank / "dsvariance" / "dsconfig.yaml", False)
    reader.dsacoustic = VoiceBankReader.DSAcoustic(bank / "dsconfig.yaml", False)
    voc_root = Path(vocoder_path).resolve() if vocoder_path else bank / "dsvocoder"
    reader.dsvocoder = VoiceBankReader.DSVocoder(voc_root / "vocoder.yaml", False)
    reader.character = None

    from diffsinger_utau.voice_bank.pred_all import PredAll
    pred = PredAll.__new__(PredAll)
    pred.voice_bank_path = bank
    pred.voice_bank_reader = reader
    pred.dsdur = reader.dsdur
    pred.dspitch = reader.dspitch
    pred.dsvariance = reader.dsvariance
    pred.dsacoustic = reader.dsacoustic
    pred.dsvocoder = reader.dsvocoder
    pred.pred_duration = PredDuration(pred.dsdur)
    pred.pred_pitch = PredPitch(pred.dspitch)
    pred.pred_variance = PredVariance(pred.dsvariance)
    pred.pred_acoustic = PredAcoustic(pred.dsacoustic)
    pred.pred_vocoder = PredVocoder(pred.dsvocoder)
    pred.available_speakers = [s.speaker_name for s in pred.dsdur.speakers] if pred.dsdur.speakers else []
    return pred
