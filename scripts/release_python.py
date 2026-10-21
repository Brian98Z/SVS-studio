from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from email.parser import Parser
from pathlib import Path

from packaging.utils import canonicalize_name

VERIFY_RUNTIME = r"""
import base64, csv, hashlib, importlib.metadata as metadata, io, json, os, platform, re, struct, sys, sysconfig, zipfile
from email.parser import Parser

if sys.implementation.name != 'cpython' or sys.version_info[:2] != (3, 11):
    raise SystemExit(f'expected CPython 3.11, found {sys.implementation.name} {sys.version.split()[0]}')
if sys.platform != 'win32' or struct.calcsize('P') != 8 or platform.machine().lower() not in ('amd64', 'x86_64'):
    raise SystemExit(f'expected Windows x64, found {sys.platform} {platform.machine()} {struct.calcsize("P") * 8}-bit')

locked = json.loads(sys.argv[1])
wheelhouse = os.path.abspath(sys.argv[2])
if not os.path.isdir(wheelhouse):
    raise SystemExit(f'Locked wheelhouse is missing: {wheelhouse}')
found = {re.sub(r'[-_.]+', '-', d.metadata['Name']).lower(): d for d in metadata.distributions() if d.metadata.get('Name')}
problems = []
allowed_paths = set()
package_roots = set()
for name, lock in sorted(locked.items()):
    version, expected_wheel_hash = lock
    dist = found.get(name)
    if dist is None:
        problems.append(f'{name}: distribution metadata missing')
        continue
    if dist.version != version:
        problems.append(f'{name}: expected version {version}, found {dist.version}')
        continue
    matches = []
    for filename in os.listdir(wheelhouse):
        if not filename.lower().endswith('.whl'):
            continue
        wheel_path = os.path.join(wheelhouse, filename)
        try:
            with zipfile.ZipFile(wheel_path) as archive:
                metadata_names = [n for n in archive.namelist() if n.endswith('.dist-info/METADATA') and n.count('/') == 1]
                if len(metadata_names) != 1:
                    continue
                wheel_metadata = Parser().parsestr(archive.read(metadata_names[0]).decode('utf-8', 'replace'))
                if (re.sub(r'[-_.]+', '-', (wheel_metadata.get('Name') or '')).lower() == name
                        and wheel_metadata.get('Version') == version):
                    matches.append((wheel_path, archive.namelist(), metadata_names[0]))
        except (OSError, zipfile.BadZipFile):
            continue
    if len(matches) != 1:
        problems.append(f'{name}: expected exactly one locked {version} wheel in {wheelhouse}, found {len(matches)}')
        continue
    wheel_path, wheel_files, metadata_name = matches[0]
    digest = hashlib.sha256()
    try:
        with open(wheel_path, 'rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
    except OSError as exc:
        problems.append(f'{name}: cannot read locked wheel: {exc}')
        continue
    if digest.hexdigest() != expected_wheel_hash:
        problems.append(f'{name}: wheel SHA-256 does not match release lock')
        continue
    files = dist.files
    if files is None:
        problems.append(f'{name}: installed distribution has no RECORD file list')
        continue
    record_name = metadata_name.rsplit('/', 1)[0] + '/RECORD'
    if record_name not in wheel_files:
        problems.append(f'{name}: locked wheel has no RECORD')
        continue
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            original_rows = {row[0]: row[1:] for row in csv.reader(io.StringIO(archive.read(record_name).decode('utf-8')))}
    except (OSError, UnicodeError, csv.Error, zipfile.BadZipFile) as exc:
        problems.append(f'{name}: cannot read original wheel RECORD: {exc}')
        continue
    installed_rows = {record.as_posix(): record for record in files}
    data_prefix = metadata_name.rsplit('/', 1)[0].rsplit('.dist-info', 1)[0] + '.data/'
    for relative in original_rows:
        if relative.startswith(data_prefix):
            category, separator, suffix = relative[len(data_prefix):].partition('/')
            scheme = {'purelib': 'purelib', 'platlib': 'platlib', 'scripts': 'scripts', 'headers': 'include', 'data': 'data'}.get(category)
            if not separator or not scheme:
                continue
            target = os.path.join(sysconfig.get_path(scheme), *suffix.split('/'))
        else:
            target = os.fspath(dist.locate_file(relative))
            first = relative.split('/', 1)[0]
            if '/' in relative:
                package_roots.add(os.path.abspath(os.fspath(dist.locate_file(first))))
        allowed_paths.add(os.path.normcase(os.path.abspath(target)))
    for generated in ('INSTALLER', 'REQUESTED', 'direct_url.json'):
        allowed_paths.add(os.path.normcase(os.path.abspath(os.fspath(dist.locate_file(metadata_name.rsplit('/', 1)[0] + '/' + generated)))))
    for relative, original in original_rows.items():
        if len(original) != 2 or not original[0]:
            continue  # Wheel RECORD rows without a hash are intentionally not provenance claims.
        if not original[1]:
            problems.append(f'{name}: locked wheel has a hash without a size: {relative}')
            continue
        try:
            original_mode, original_value = original[0].split('=', 1)
            original_size = int(original[1])
        except (ValueError, AttributeError):
            problems.append(f'{name}: invalid locked wheel RECORD entry: {relative}')
            continue
        # Wheel installers must replace the standard script placeholder shebang.
        # Derive the expected bytes from the locked original, never from installed RECORD.
        if relative.startswith(data_prefix + 'scripts/'):
            with zipfile.ZipFile(wheel_path) as archive:
                script = archive.read(relative)
            first, separator, rest = script.partition(b'\n')
            if first.rstrip(b'\r') in (b'#!python', b'#!pythonw'):
                executable = sys.executable
                if first.rstrip(b'\r') == b'#!pythonw':
                    executable = os.path.join(os.path.dirname(executable), 'pythonw.exe')
                ending = b'\r\n' if first.endswith(b'\r') else b'\n'
                script = b'#!' + executable.encode('utf-8') + ending + rest
                original_value = base64.urlsafe_b64encode(hashlib.new(original_mode, script).digest()).decode('ascii').rstrip('=')
                original_size = len(script)
        installed_path = relative
        if relative.startswith(data_prefix):
            data_path = relative[len(data_prefix):]
            category, separator, suffix = data_path.partition('/')
            scheme = {'purelib': 'purelib', 'platlib': 'platlib', 'scripts': 'scripts',
                      'headers': 'include', 'data': 'data'}.get(category)
            if separator and scheme:
                target = os.path.join(sysconfig.get_path(scheme), *suffix.split('/'))
                installed_path = os.path.relpath(target, dist.locate_file('')).replace(os.sep, '/')
        record = installed_rows.get(installed_path)
        if record is None:
            problems.append(f'{name}: locked wheel file missing from installed RECORD: {relative}')
            continue
        record_hash = record.hash
        if record_hash is None:
            problems.append(f'{name}: installed RECORD dropped locked wheel hash: {relative}')
            continue
        if record_hash.mode != original_mode or record_hash.value != original_value:
            problems.append(f'{name}: installed RECORD hash differs from locked wheel RECORD: {relative}')
            continue
        if record.size is not None and record.size != original_size:
            problems.append(f'{name}: installed RECORD size differs from locked wheel RECORD: {relative}')
            continue
        path = dist.locate_file(record)
        if not path.is_file():
            problems.append(f'{name}: missing locked wheel file {relative}')
            continue
        try:
            actual = hashlib.new(original_mode)
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    actual.update(chunk)
            actual_size = path.stat().st_size
        except (OSError, ValueError) as exc:
            problems.append(f'{name}: cannot verify {relative}: {exc}')
            continue
        actual_hash = base64.urlsafe_b64encode(actual.digest()).decode('ascii').rstrip('=')
        if actual_hash != original_value or actual_size != original_size:
            problems.append(f'{name}: installed file differs from locked wheel: {relative}')
# collect-all walks package directories, not only RECORD. Reject undeclared
# source/data added beside intact locked files before PyInstaller can ship it.
for root in sorted(package_roots):
    for directory, children, filenames in os.walk(root, followlinks=False):
        for child in list(children):
            if os.path.islink(os.path.join(directory, child)):
                problems.append('symlink in locked package tree: ' + os.path.join(directory, child))
                children.remove(child)
        for filename in filenames:
            candidate = os.path.join(directory, filename)
            if filename.endswith('.pyc') and os.path.basename(directory) == '__pycache__':
                continue  # Interpreter-generated cache; source remains wheel-verified.
            if os.path.normcase(os.path.abspath(candidate)) not in allowed_paths:
                problems.append('untracked file in locked package tree: ' + candidate)
if problems:
    raise SystemExit('Installed wheel provenance failure: ' + '; '.join(problems))
print('Locked wheel provenance verified')
"""

def check_runtime(python: Path, expected: dict[str, tuple[str, str]], wheelhouse: Path) -> None:
    result = run([str(python), "-c", VERIFY_RUNTIME, json.dumps(expected, sort_keys=True), str(wheelhouse)], check=False)
    if result.returncode:
        raise SystemExit(result.stderr.strip() or result.stdout.strip() or "Locked wheel verification failed")



def parse_lock() -> dict[str, tuple[str, str]]:
    expected: dict[str, tuple[str, str]] = {}
    for number, line in enumerate(LOCK.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s]+) --hash=sha256:([0-9a-f]{64})", line)
        if not match:
            raise SystemExit(f"Invalid lock entry at {LOCK}:{number}: {line!r}")
        name = canonicalize_name(match.group(1))
        if name in expected:
            raise SystemExit(f"Duplicate lock entry at {LOCK}:{number}: {match.group(1)}")
        expected[name] = (match.group(2), match.group(3))
    if not expected:
        raise SystemExit(f"No locked distributions parsed from {LOCK}")
    return expected


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VENV = ROOT / "apps/desktop/src-tauri/.venv-release-lock/Scripts/python.exe"
LOCK = ROOT / "scripts/release-windows-py311.lock"
INPUTS = ROOT / "release/python"
DEFAULT_WHEELHOUSE = ROOT / "release/python/wheelhouse"


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, text=True, capture_output=True)


def installed(python: Path) -> dict[str, object]:
    raw = run([str(python), "-m", "pip", "list", "--format=json"]).stdout
    return {canonicalize_name(row["name"]): row["version"] for row in json.loads(raw)}


def make_lock(args: argparse.Namespace) -> None:
    python = Path(args.python).resolve()
    if not python.is_file():
        raise SystemExit(f"Python build environment is missing: {python}")
    wheelhouse = Path(args.wheelhouse).resolve()
    wheelhouse.mkdir(parents=True, exist_ok=True)
    if any(wheelhouse.iterdir()):
        raise SystemExit(f"Wheelhouse must be empty before lock generation: {wheelhouse}")
    check_runtime(python, {}, wheelhouse)
    run([str(python), "-m", "pip", "download", "--only-binary=:all:", "--dest", str(wheelhouse), "-r", str(ROOT / "requirements-build.txt")])
    records: dict[str, tuple[str, str, str]] = {}
    for artifact in wheelhouse.iterdir():
        if not artifact.is_file() or artifact.suffix != ".whl":
            raise RuntimeError(f"Unexpected non-wheel artifact in binary wheelhouse: {artifact.name}")
        with zipfile.ZipFile(artifact) as archive:
            metadata_name = next((n for n in archive.namelist() if n.endswith(".dist-info/METADATA")), None)
            if not metadata_name:
                raise RuntimeError(f"Wheel has no METADATA: {artifact.name}")
            metadata = Parser().parsestr(archive.read(metadata_name).decode("utf-8", "replace"))
        name, version = metadata["Name"], metadata["Version"]
        key = canonicalize_name(name)
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        candidate = (name, version, digest)
        prior = records.get(key)
        if prior and prior != candidate:
            raise RuntimeError(f"Multiple versions/artifacts for {name}: {prior[1]} and {version}")
        records[key] = candidate
    if not records:
        raise RuntimeError("pip download produced no wheel artifacts")
    current = installed(python)
    missing = sorted(name for name in records if name not in current or current[name] != records[name][1])
    if missing:
        raise RuntimeError("Downloaded closure missing or version-mismatched in release environment: " + ", ".join(missing))
    output = ["# Windows x64 / CPython 3.11 release runtime; hashes are the downloaded wheel SHA-256.",
              "# Regenerate with scripts/release_python.py lock --wheelhouse <empty-directory>."]
    for key in sorted(records):
        name, version, digest = records[key]
        output.append(f"{name}=={version} --hash=sha256:{digest}")
    LOCK.write_text("\n".join(output) + "\n", encoding="utf-8", newline="\n")
    print(f"Wrote {len(records)} pinned distributions to {LOCK}")


def verify(args: argparse.Namespace) -> None:
    python = Path(args.python).resolve()
    expected = parse_lock()
    wheelhouse = Path(args.wheelhouse).resolve()
    check_runtime(python, expected, wheelhouse)
    current = installed(python)
    extra = sorted(set(current) - set(expected) - {"pip", "setuptools"})
    if extra:
        raise SystemExit("Build environment differs from release lock: unexpected distributions: " + ", ".join(extra))
    print(f"Verified {len(expected)} locked package versions and installed files against original hash-locked wheels.")


def inventory(args: argparse.Namespace) -> None:
    python = Path(args.python).resolve()
    rows = [{"name": name, "version": version} for name, version in sorted(installed(python).items())]
    INPUTS.mkdir(parents=True, exist_ok=True)
    pyversion = run([str(python), "-c", "import sys; print(sys.version)"]).stdout.strip()
    (INPUTS / "python-inventory.json").write_text(json.dumps({"python": pyversion, "platform": sys.platform, "packages": rows}, indent=2) + "\n", encoding="utf-8")
    print(f"Recorded {len(rows)} installed distributions in {INPUTS / 'python-inventory.json'}")




def license_inputs(args: argparse.Namespace) -> None:
    python = Path(args.python).resolve()
    dest = Path(args.out).resolve()
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    rows = []
    # Resolve actual installed package metadata without changing the environment.
    code = """
import importlib.metadata as metadata
import json
import sysconfig
from pathlib import Path, PurePosixPath

def is_license_text(path):
    parts = PurePosixPath(path).parts
    if any(part.lower() == '__pycache__' for part in parts):
        return False
    name = parts[-1].lower()
    if name.endswith(('.py', '.pyc', '.pyo', '.pyd')):
        return False
    in_metadata_licenses = any(
        part.lower() == 'licenses' and index > 0 and parts[index - 1].lower().endswith('.dist-info')
        for index, part in enumerate(parts)
    )
    named_license = any(word in name for word in ('license', 'copying', 'notice', 'authors'))
    if not in_metadata_licenses and not named_license:
        return False
    try:
        content = (site_packages / path).read_bytes()
        if b'\\0' in content:
            return False
        content.decode('utf-8')
    except (OSError, UnicodeDecodeError):
        return False
    return True

site_packages = Path(sysconfig.get_paths()['purelib']).resolve()

print(json.dumps([
    {'name': dist.metadata.get('Name'), 'version': dist.version,
     'files': [str(file) for file in (dist.files or []) if is_license_text(str(file))]}
    for dist in metadata.distributions()
]))
"""
    distributions = json.loads(run([str(python), "-c", code]).stdout)
    for dist in distributions:
        matches = []
        for item in dist["files"]:
            path = Path(python).parent.parent / "Lib/site-packages" / item
            if path.is_file():
                matches.append(path)
        copied = []
        for source in matches:
            relative = source.relative_to(Path(python).parent.parent / "Lib/site-packages")
            rel = Path(canonicalize_name(dist["name"])) / relative
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(str(rel).replace(os.sep, "/"))
        metadata = run([str(python), "-c", f"import importlib.metadata as m,json; d=m.distribution({dist['name']!r}); print(json.dumps({{'license':d.metadata.get('License'),'project_urls':d.metadata.get_all('Project-URL') or []}}))"]).stdout
        license_facts = json.loads(metadata)
        rows.append({"name": dist["name"], "version": dist["version"], "license_files": copied,
                     "declared_license_metadata": license_facts["license"], "project_urls": license_facts["project_urls"],
                     "license_metadata": "Requires manual verification; package metadata is not a legal conclusion."})
    inventory_path = dest / "THIRD-PARTY-INVENTORY.json"
    inventory_path.write_text(json.dumps({"distributions": rows}, indent=2) + "\n", encoding="utf-8")
    (dest / "THIRD-PARTY-NOTICE.txt").write_text(
        "Pedalboard 0.9.26 is GPL-3.0-or-later. The matching upstream tag archive and exact five upstream gitlink commit source archives, with discovered license texts, are included under pedalboard-0.9.26-source.tar.gz and pedalboard-submodules/. Provenance and SHA-256 are in PEDALBOARD-SOURCE-STATUS.json and pedalboard-submodules/SUBMODULE-INVENTORY.json. These are upstream source candidates, not proven to be the exact source used to produce the PyPI Windows wheel; no legal compliance determination is made. Review source/build correspondence, embedded native components and all GPL obligations before redistribution. A source URL alone is not fulfillment.\n", encoding="utf-8")
    print(f"Collected discovered license files for {len(rows)} distributions at {dest}")


def pedalboard_source(args: argparse.Namespace) -> None:
    python = Path(args.python).resolve()
    version = installed(python).get("pedalboard")
    if not version:
        raise SystemExit("Pedalboard is not installed in the frozen environment")
    dest = Path(args.out).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    run([str(python), "-m", "pip", "download", "--no-deps", "--no-binary=:all:", "--dest", str(dest), f"pedalboard=={version}"])
    files = list(dest.glob("pedalboard-*.tar.gz")) + list(dest.glob("pedalboard-*.zip"))
    if len(files) != 1:
        raise RuntimeError(f"Expected one Pedalboard {version} source archive, found {len(files)}")
    digest = hashlib.sha256(files[0].read_bytes()).hexdigest()
    record = {"distribution": "pedalboard", "installed_version": version, "source_archive": files[0].name,
              "sha256": digest, "source_version_verified_by_pip_resolution": True,
              "compliance_status": "source archive acquired; completeness and adequacy of corresponding source remain subject to GPL-3.0 legal review"}
    (dest / "PEDALBOARD-CORRESPONDING-SOURCE.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare and validate desktop Python release inputs.")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("lock", help="Resolve and hash downloaded Windows CPython 3.11 wheels")
    p.add_argument("--python", default=str(DEFAULT_VENV))
    p.add_argument("--wheelhouse", required=True)
    p.set_defaults(func=make_lock)
    p = commands.add_parser("verify", help="Check installed files against original hash-locked wheels")
    p.add_argument("--python", default=str(DEFAULT_VENV))
    p.add_argument("--wheelhouse", default=str(DEFAULT_WHEELHOUSE))
    p.set_defaults(func=verify)
    p = commands.add_parser("inventory", help="Record installed distribution names and versions")
    p.add_argument("--python", default=str(DEFAULT_VENV))
    p.set_defaults(func=inventory)
    p = commands.add_parser("licenses", help="Collect discovered third-party license/notice files")
    p.add_argument("--python", default=str(DEFAULT_VENV))
    p.add_argument("--out", required=True)
    p.set_defaults(func=license_inputs)
    p = commands.add_parser("pedalboard-source", help="Acquire the exact installed Pedalboard source archive")
    p.add_argument("--python", default=str(DEFAULT_VENV))
    p.add_argument("--out", required=True)
    p.set_defaults(func=pedalboard_source)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
