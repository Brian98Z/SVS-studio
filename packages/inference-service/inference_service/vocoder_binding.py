"""Validation and assembly for user-owned external DiffSinger vocoders."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

import re
from pathlib import PureWindowsPath


_DSCONFIG_REFERENCES = {
    "dsconfig.yaml": {"acoustic": Path(".")},
    "dsdur/dsconfig.yaml": {"linguistic": Path("dsdur"), "dur": Path("dsdur")},
    "dspitch/dsconfig.yaml": {"linguistic": Path("dspitch"), "pitch": Path("dspitch")},
    "dsvariance/dsconfig.yaml": {"linguistic": Path("dsvariance"), "variance": Path("dsvariance")},
}


def validate_dsconfig_references(voicebank: Path) -> list[str]:
    """Validate every acoustic/component model reference before probing any target."""
    root = Path(voicebank).resolve()
    targets: list[tuple[str, Path]] = []
    for config_name, references in _DSCONFIG_REFERENCES.items():
        config = root / config_name
        data = _yaml(config)
        for key, base in references.items():
            ref = data.get(key)
            if not isinstance(ref, str) or not ref or "\x00" in ref:
                raise ValueError(f"invalid {config_name}:{key} model reference")
            windows = PureWindowsPath(ref)
            if (windows.root or windows.drive or Path(ref).is_absolute()
                    or re.match(r"^[\\/]{2,}[?.]", ref)):
                raise ValueError(f"non-local {config_name}:{key} model reference")
            target = (root / base / Path(ref.replace("\\", "/"))).resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"{config_name}:{key} model reference escapes voicebank")
            targets.append((f"{config_name}:{key}", target))
    return [str(target) for _, target in targets if not target.is_file()]


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
    if isinstance(ref, str) and (PureWindowsPath(ref).root or PureWindowsPath(ref).drive or Path(ref).is_absolute() or "\x00" in ref):
        issues.append("model reference must be a local relative path")
        model = None
    else:
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
    """Compose upstream supported readers without PredAll's hardcoded vocoder."""
    bank = voicebank.resolve()
    validate_dsconfig_references(bank)
    # Validate references before importing or constructing any external predictor.
    from diffsinger_utau.voice_bank.commons.voice_bank_reader import VoiceBankReader
    from diffsinger_utau.voice_bank.pred_duration import PredDuration
    from diffsinger_utau.voice_bank.pred_pitch import PredPitch
    from diffsinger_utau.voice_bank.pred_variance import PredVariance
    from diffsinger_utau.voice_bank.pred_acoustic import PredAcoustic
    from diffsinger_utau.voice_bank.pred_vocoder import PredVocoder
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
