#!/usr/bin/env python3
"""Build or verify the two reviewable, self-contained offline plugins."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from os import environ
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "packages" / "manifest.json"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
UV_CONFIG = """[tool.uv]
no-index = true
find-links = ["./wheels"]
environments = ["sys_platform == 'linux' and platform_machine == 'x86_64'"]
"""


def load_manifest() -> list[dict[str, Any]]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["plugins"]


def resolve_inside(relative_path: str) -> Path:
    path = (ROOT / relative_path).resolve()
    if not path.is_relative_to(ROOT):
        raise RuntimeError(f"Path escapes bundled plugin root: {relative_path}")
    return path


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_wheel_lock(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        digest, filename = line.split(maxsplit=1)
        if (
            len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or "/" in filename
            or filename in checksums
        ):
            raise RuntimeError(f"Invalid wheel lock entry: {line}")
        checksums[filename] = digest
    return checksums


def strip_toml_section(text: str, section: str) -> str:
    pattern = re.compile(rf"(?ms)^\[{re.escape(section)}\]\s*\n.*?(?=^\[|\Z)")
    return pattern.sub("", text).rstrip() + "\n"


def download_wheels(plugin: dict[str, Any], destination: Path) -> None:
    requirements = resolve_inside(plugin["requirements_lock"])
    expected = load_wheel_lock(resolve_inside(plugin["wheel_lock"]))
    download_requirements = destination.parent / "requirements.txt"
    lines = [
        line for line in requirements.read_text(encoding="utf-8").splitlines() if line and not line.startswith("--")
    ]
    download_requirements.write_text("\n".join(lines) + "\n", encoding="utf-8")
    destination.mkdir()

    python_version = plugin["python_version"]
    command = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--requirement",
        str(download_requirements),
        "--dest",
        str(destination),
        "--only-binary=:all:",
        "--python-version",
        python_version,
        "--implementation",
        "cp",
        "--abi",
        f"cp{python_version.replace('.', '')}",
        "--index-url",
        environ.get("PIP_INDEX_URL", "https://pypi.org/simple"),
        "--quiet",
    ]
    for platform in plugin["pip_platforms"]:
        command.extend(("--platform", platform))
    subprocess.run(command, check=True)

    actual = {path.name: file_sha256(path) for path in destination.glob("*.whl")}
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        changed = sorted(name for name in set(actual) & set(expected) if actual[name] != expected[name])
        raise RuntimeError(
            f"Downloaded wheel lock mismatch for {plugin['plugin_id']}: "
            f"missing={missing}, unexpected={unexpected}, changed={changed}"
        )


def prepare_plugin(plugin: dict[str, Any], destination: Path, wheels: Path) -> None:
    source = resolve_inside(plugin["source_dir"])
    if tree_sha256(source) != plugin["source_tree_sha256"]:
        raise RuntimeError(f"Source tree checksum mismatch: {plugin['plugin_id']}")
    shutil.copytree(source, destination)
    (destination / "wheels.sha256").unlink()

    pyproject = destination / "pyproject.toml"
    content = strip_toml_section(pyproject.read_text(encoding="utf-8"), "dependency-groups")
    content = strip_toml_section(content, "tool.uv")
    pyproject.write_text(content.rstrip() + "\n\n" + UV_CONFIG, encoding="utf-8")
    for ignore_name in (".difyignore", ".gitignore"):
        ignore = destination / ignore_name
        if ignore.is_file():
            lines = [
                line for line in ignore.read_text(encoding="utf-8").splitlines() if line not in {"wheels", "wheels/"}
            ]
            ignore.write_text("\n".join(lines) + "\n", encoding="utf-8")
    shutil.copytree(wheels, destination / "wheels")


def write_archive(source: Path, output: Path) -> None:
    temporary_output = output.with_suffix(output.suffix + ".tmp")
    temporary_output.unlink(missing_ok=True)
    with zipfile.ZipFile(temporary_output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            info = zipfile.ZipInfo(path.relative_to(source).as_posix(), ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    temporary_output.replace(output)


def build() -> None:
    for plugin in load_manifest():
        with tempfile.TemporaryDirectory(prefix="dify-bundled-plugin-") as temporary:
            temporary_path = Path(temporary)
            wheels = temporary_path / "wheels"
            prepared = temporary_path / "plugin"
            download_wheels(plugin, wheels)
            prepare_plugin(plugin, prepared, wheels)
            output = ROOT / "packages" / plugin["file"]
            write_archive(prepared, output)
            sys.stdout.write(f"built {output.relative_to(ROOT)}\n")


def manifest_value(text: str, key: str) -> str:
    match = re.search(rf"(?m)^{re.escape(key)}:\s*[\"']?([^\s\"']+)", text)
    if match is None:
        raise RuntimeError(f"Missing {key} in plugin manifest")
    return match.group(1)


def verify() -> None:
    loose_wheels = list(ROOT.rglob("*.whl"))
    if loose_wheels:
        raise RuntimeError(f"Wheels must only exist inside generated difypkg archives: {loose_wheels}")
    for plugin in load_manifest():
        plugin_id = plugin["plugin_id"]
        source = resolve_inside(plugin["source_dir"])
        package = ROOT / "packages" / plugin["file"]
        wheel_lock = load_wheel_lock(resolve_inside(plugin["wheel_lock"]))
        if tree_sha256(source) != plugin["source_tree_sha256"]:
            raise RuntimeError(f"Source tree checksum mismatch: {plugin_id}")
        if file_sha256(package) != plugin["sha256"]:
            raise RuntimeError(f"Runtime package checksum mismatch: {plugin_id}")

        with zipfile.ZipFile(package) as archive:
            if archive.testzip() is not None or any(name.endswith("/") for name in archive.namelist()):
                raise RuntimeError(f"Invalid ZIP structure: {plugin_id}")
            names = archive.namelist()
            if ".verification.dify.json" in names or "uv.lock" in names:
                raise RuntimeError(f"Invalid upstream signature or online lock retained: {plugin_id}")
            packaged_wheels = {name.removeprefix("wheels/") for name in names if name.startswith("wheels/")}
            if packaged_wheels != set(wheel_lock):
                raise RuntimeError(f"Wheel set mismatch: {plugin_id}")
            for filename, digest in wheel_lock.items():
                if hashlib.sha256(archive.read(f"wheels/{filename}")).hexdigest() != digest:
                    raise RuntimeError(f"Wheel checksum mismatch in {plugin_id}: {filename}")
            if not archive.read("requirements.txt").decode().startswith("--no-index --find-links=./wheels/\n"):
                raise RuntimeError(f"Offline requirements configuration missing: {plugin_id}")
            pyproject = archive.read("pyproject.toml").decode()
            for expected in ("no-index = true", 'find-links = ["./wheels"]', "platform_machine == 'x86_64'"):
                if expected not in pyproject:
                    raise RuntimeError(f"Offline uv configuration missing from {plugin_id}: {expected}")
            manifest = archive.read("manifest.yaml").decode()

        author, name = plugin_id.split("/", 1)
        actual = (
            manifest_value(manifest, "author"),
            manifest_value(manifest, "name"),
            manifest_value(manifest, "version"),
        )
        if actual != (author, name, plugin["version"]):
            raise RuntimeError(f"Plugin manifest identity mismatch: {plugin_id}")
        sys.stdout.write(f"verified {plugin_id}:{plugin['version']} ({len(wheel_lock)} wheels)\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("build", "verify"))
    arguments = parser.parse_args()
    build() if arguments.action == "build" else verify()


if __name__ == "__main__":
    main()
