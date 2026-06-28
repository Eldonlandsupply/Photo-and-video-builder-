#!/usr/bin/env python3
"""Create a portable download bundle for this ComfyUI node pack.

The bundle includes the core node files, examples, config, README, license,
and submodule metadata. It does not vendor optional submodule contents or model
weights, so the archive stays small and redistributable.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "dist" / "comfyui-photoreal-prompt-builder-download.zip"

INCLUDE_PATHS = (
    "README.md",
    "LICENSE",
    "pyproject.toml",
    "config.json",
    "__init__.py",
    "nodes.py",
    "list_nodes.py",
    "vlm_nodes.py",
    ".gitmodules",
    "examples/xy_plot_minimal.json",
    "examples/xy_plot_sfw.json",
)


def resolve_output(path_text: str) -> Path:
    """Resolve an output path under the repository root.

    Rejects absolute paths, parent-directory traversal, and symlink escapes.
    """
    raw = Path(path_text)
    if raw.is_absolute():
        raise ValueError("output path must be relative to the repository root")
    if any(part == ".." for part in raw.parts):
        raise ValueError("output path must not contain '..' segments")

    target = (REPO_ROOT / raw).resolve()
    root = REPO_ROOT.resolve()
    if os.path.commonpath([str(root), str(target)]) != str(root):
        raise ValueError("output path escapes the repository root")

    parent = target.parent
    if parent.exists() and parent.is_symlink():
        raise ValueError("output parent must not be a symlink")
    parent.mkdir(parents=True, exist_ok=True)
    return target


def add_file(bundle: zipfile.ZipFile, rel_path: str) -> None:
    source = (REPO_ROOT / rel_path).resolve()
    root = REPO_ROOT.resolve()
    if os.path.commonpath([str(root), str(source)]) != str(root):
        raise ValueError(f"refusing to package path outside repo: {rel_path}")
    if not source.is_file():
        raise FileNotFoundError(f"required bundle file missing: {rel_path}")
    bundle.write(source, rel_path)


def create_bundle(output: Path) -> None:
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for rel_path in INCLUDE_PATHS:
            add_file(bundle, rel_path)
        bundle.writestr(
            "DOWNLOAD_README.txt",
            "ComfyUI Photoreal Prompt Builder download bundle\n"
            "\n"
            "Install by extracting this archive into ComfyUI/custom_nodes/"
            "comfyui-klein-photoreal-promptbuilder.\n"
            "\n"
            "Optional module:\n"
            "1. If installed from git, run: git submodule update --init --recursive\n"
            "2. Set config.json nsfw to true only if you intentionally enable adult nodes.\n"
            "3. Restart ComfyUI.\n"
            "\n"
            "This archive does not include generated media, model weights, or optional "
            "submodule contents.\n",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a portable download ZIP bundle.")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT.relative_to(REPO_ROOT)),
        help="relative output path under the repository root",
    )
    args = parser.parse_args()

    output = resolve_output(args.output)
    create_bundle(output)
    print(output.relative_to(REPO_ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
