"""Build the explicitly versioned CUDA dependency variant; never patch an installed package."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile

SOURCE_URL = "https://files.pythonhosted.org/packages/7e/4e/66dbceb2c3f9cf3c9bf3cc7cb2f41c87071ab6c1f27b1ad9b9392344b794/diffsinger_utau-0.3.8.tar.gz"
SOURCE_SHA256 = "a16cdf8a4341ed542cf6ce2936c80ebb0f8c8eed9c9c31d8f41b7c688db9fdf0"
VERSION = "0.3.8+svscuda1"
ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "build/native-cuda/wheels")
    parser.add_argument("--temp-dir", type=Path, default=Path("F:/OMP/temp"))
    args = parser.parse_args()
    args.out_dir = args.out_dir.resolve()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.temp_dir.mkdir(parents=True, exist_ok=True)
    wheel_name = f"diffsinger_utau-{VERSION}-py3-none-any.whl"
    if (args.out_dir / wheel_name).exists():
        raise FileExistsError(f"Refusing to replace existing wheel: {args.out_dir / wheel_name}")
    with urllib.request.urlopen(SOURCE_URL, timeout=60) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != SOURCE_SHA256:
        raise ValueError("Upstream source SHA-256 mismatch")
    with tempfile.TemporaryDirectory(prefix="svs-native-cuda-", dir=args.temp_dir) as work:
        work = Path(work)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
            for member in source.getmembers():
                destination = (work / member.name).resolve()
                if not destination.is_relative_to(work.resolve()) or not (member.isfile() or member.isdir()):
                    raise ValueError(f"Unsafe source archive member: {member.name}")
            source.extractall(work, filter="data")
        project = work / "diffsinger_utau-0.3.8"
        original = {p.relative_to(project).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in project.rglob("*.py")}
        metadata = project / "pyproject.toml"
        text = metadata.read_text(encoding="utf-8")
        start = text.index("dependencies = [")
        end = text.index("\n]\n", start) + len("\n]")
        text = text[:start] + '''dependencies = [
  "setuptools",
  "numpy==1.26.4",
  "scipy==1.13.1",
  "librosa==0.10.2.post1",
  "PyYAML>=6.0,<7",
  "onnxruntime-gpu==1.31.0",
  "torch==2.11.0+cu130",
  "pypinyin>=0.53,<1"
]''' + text[end:]
        text = text.replace('dynamic = ["version"]', f'version = "{VERSION}"')
        text = text.replace('requires-python = ">=3.9"', 'requires-python = ">=3.11,<3.12"')
        text = text.replace('[tool.setuptools.dynamic]\nversion = {attr = "diffsinger_utau.voice_bank.__version__"}\n', '')
        metadata.write_text(text, encoding="utf-8", newline="\n")
        subprocess.run(["uv", "build", "--python", "3.11", "--wheel", "--out-dir",
                        str(args.out_dir), str(project)], check=True)
        wheel = args.out_dir / wheel_name
        with zipfile.ZipFile(wheel) as built:
            for name, digest in original.items():
                if name.startswith("diffsinger_utau/") and hashlib.sha256(built.read(name)).hexdigest() != digest:
                    raise ValueError(f"Runtime source changed: {name}")
            if not any(name.endswith("/licenses/LICENSE") for name in built.namelist()):
                raise ValueError("Upstream license missing from wheel")
        manifest = {
            "source_url": SOURCE_URL, "source_sha256": SOURCE_SHA256,
            "distribution_version": VERSION,
            "change": "Build-time dependency metadata only: Windows Python 3.11 CUDA profile; runtime Python unchanged.",
            "runtime_version_note": "Upstream module __version__ stays 0.3.8; distribution version identifies this derivative.",
            "wheel": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "runtime_source_sha256": {k: v for k, v in original.items() if k.startswith("diffsinger_utau/")},
            "license": "Upstream MIT retained; this does not license models or voicebanks."
        }
        (args.out_dir / "source-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({k: manifest[k] for k in ("wheel", "sha256", "source_sha256")}, indent=2))


if __name__ == "__main__":
    main()
