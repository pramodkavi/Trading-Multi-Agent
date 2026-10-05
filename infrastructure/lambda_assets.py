"""Build the Lambda code asset (zip) and the shared dependency layer.

Design: docs/superpowers/specs/2026-10-03-lambda-zip-repackage-design.md §3.

Why this exists: the Lambda console inline editor only works for a zip
deployment package under 3 MB, and function + layers must stay under 250 MB
unzipped. So the deployable is split into

  * a **code asset** -- ``src/`` + ``scripts/`` copied verbatim (~0.6 MB), and
  * a **dependency layer** -- every ``[project].dependencies`` entry from
    ``pyproject.toml`` except ``boto3`` (already in the Lambda runtime) and
    ``psycopg`` (used only by ``scripts/migrate.py``'s local socket path and
    the integration tests; nothing under ``src/`` imports it).

Both are produced by CDK *local bundling* (no Docker): pip resolves manylinux
wheels for the Lambda platform directly, which works on Windows and on the
GitHub runners. A bundling failure RAISES -- it never returns False, because
that would make CDK silently fall back to Docker.

Size guards fail ``cdk synth`` loudly if either cap is approached, naming the
largest packages, instead of letting a deploy succeed and the console editor
silently disappear.

The layer asset hash is CUSTOM (sha256 of the filtered requirement list +
python version + platform), so the ~190 MB layer is rebuilt and re-uploaded
only when dependencies change; code-only deploys ship just the small zip.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import jsii
from aws_cdk import AssetHashType, BundlingOptions, ILocalBundling
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

# infrastructure/lambda_assets.py -> parents[1] is the repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]

CODE_DIRS: tuple[str, ...] = ("src", "scripts")
LAYER_EXCLUDES: frozenset[str] = frozenset({"boto3", "psycopg"})

PYTHON_VERSION = "3.11"
PLATFORM = "manylinux2014_x86_64"
RUNTIME = lambda_.Runtime.PYTHON_3_11
ARCHITECTURE = lambda_.Architecture.X86_64

# Console inline editor needs the zipped package < 3 MB; we guard the
# UNzipped tree at 2.5 MB, which is stricter (zip is always smaller).
CODE_MAX_BYTES = int(2.5 * 1024 * 1024)
# Lambda hard cap is 250 MB unzipped for function + layers; keep 10 MB headroom.
LAYER_MAX_BYTES = 240 * 1024 * 1024

_NAME_RE = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")


class AssetTooLargeError(RuntimeError):
    """A Lambda asset exceeded its size guard (see CODE_MAX_BYTES / LAYER_MAX_BYTES)."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def normalize_name(name: str) -> str:
    """PEP 503 normalisation: case-insensitive, runs of ``-_.`` collapse to ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(spec: str) -> str:
    """Bare normalized project name of a PEP 508 requirement string.

    ``"psycopg[binary]>=3.2"`` -> ``"psycopg"``; extras, specifiers and
    environment markers are ignored. The *spec* itself is passed to pip
    unchanged by callers.
    """
    match = _NAME_RE.match(spec)
    if not match:
        raise ValueError(f"not a requirement: {spec!r}")
    return normalize_name(match.group(1))


def layer_requirements(pyproject_text: str, exclude: frozenset[str] = LAYER_EXCLUDES) -> list[str]:
    """The sorted ``[project].dependencies`` entries that belong in the layer."""
    data = tomllib.loads(pyproject_text)
    try:
        deps: list[str] = data["project"]["dependencies"]
    except KeyError as exc:
        raise ValueError("pyproject.toml has no [project].dependencies list") from exc
    excluded = {normalize_name(n) for n in exclude}
    return sorted(spec for spec in deps if requirement_name(spec) not in excluded)


def layer_asset_hash(requirements: Sequence[str]) -> str:
    """Deterministic sha256 over the requirement set + python version + platform."""
    payload = "\n".join(sorted(requirements)) + f"\n{PYTHON_VERSION}\n{PLATFORM}\n"
    return hashlib.sha256(payload.encode()).hexdigest()


def pip_install_command(
    target: Path, requirements: Sequence[str], *, python: str = sys.executable
) -> list[str]:
    """The pip command that installs manylinux wheels for the Lambda platform."""
    return [
        python,
        "-m",
        "pip",
        "install",
        "--quiet",
        "--no-compile",
        "--disable-pip-version-check",
        "--target",
        str(target / "python"),
        "--platform",
        PLATFORM,
        "--only-binary=:all:",
        "--python-version",
        PYTHON_VERSION,
        "--implementation",
        "cp",
        *requirements,
    ]


def run_pip(cmd: list[str]) -> None:
    """Default runner: execute pip, raising CalledProcessError (with output) on failure."""
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def strip_pycache(root: Path) -> None:
    """Remove every ``__pycache__`` directory and stray ``*.pyc`` under *root*."""
    for cache in sorted(root.rglob("__pycache__"), key=lambda p: -len(p.parts)):
        shutil.rmtree(cache, ignore_errors=True)
    for pyc in root.rglob("*.pyc"):
        pyc.unlink(missing_ok=True)


def top_entries(path: Path, count: int = 5) -> list[tuple[str, int]]:
    """Largest immediate children of *path* as ``(name, bytes)``, biggest first."""
    sizes = [(p.name, dir_size(p) if p.is_dir() else p.stat().st_size) for p in path.iterdir()]
    return sorted(sizes, key=lambda item: -item[1])[:count]


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def assert_size(path: Path, limit: int, what: str) -> None:
    size = dir_size(path)
    if size <= limit:
        return
    biggest = ", ".join(f"{name} {_mb(n)}" for name, n in top_entries(path))
    raise AssetTooLargeError(
        f"{what} is {_mb(size)} unzipped, over the {_mb(limit)} guard. Largest: {biggest}. "
        "See docs/superpowers/specs/2026-10-03-lambda-zip-repackage-design.md §3.2 / §4."
    )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def copy_code(output_dir: Path, *, repo_root: Path = REPO_ROOT) -> None:
    """Copy ``src/`` and ``scripts/`` into *output_dir* (the zip root), no caches."""
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".mypy_cache")
    for name in CODE_DIRS:
        shutil.copytree(repo_root / name, output_dir / name, ignore=ignore, dirs_exist_ok=True)
    strip_pycache(output_dir)
    assert_size(output_dir, CODE_MAX_BYTES, "Lambda code asset (src/ + scripts/)")


def build_layer(
    output_dir: Path,
    requirements: Sequence[str],
    *,
    runner: Callable[[list[str]], None] = run_pip,
) -> None:
    """pip-install *requirements* for the Lambda platform into ``output_dir/python``."""
    runner(pip_install_command(output_dir, requirements))
    python_dir = output_dir / "python"
    strip_pycache(python_dir)
    assert_size(python_dir, LAYER_MAX_BYTES, "Lambda dependency layer")


def read_layer_requirements(repo_root: Path | None = None) -> list[str]:
    # Resolve REPO_ROOT at call time (not as a bound default) so tests can
    # monkeypatch the module attribute.
    root = repo_root if repo_root is not None else REPO_ROOT
    return layer_requirements((root / "pyproject.toml").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# CDK glue (local bundling -- no Docker)
# ---------------------------------------------------------------------------

# jsii hands the BundlingOptions struct to Python callbacks either as one
# positional struct or expanded into keyword args depending on the jsii
# version; accept both and ignore them (we only need output_dir).


@jsii.implements(ILocalBundling)
class _CodeBundler:
    def try_bundle(self, output_dir: str, *_args: Any, **_options: Any) -> bool:
        copy_code(Path(output_dir))
        return True  # on failure copy_code RAISES; never return False (Docker fallback)


@jsii.implements(ILocalBundling)
class _LayerBundler:
    def __init__(self, requirements: Sequence[str]) -> None:
        self._requirements = list(requirements)

    def try_bundle(self, output_dir: str, *_args: Any, **_options: Any) -> bool:
        build_layer(Path(output_dir), self._requirements)
        return True


def code_asset() -> lambda_.Code:
    """A fresh ``AssetCode`` of ``src/`` + ``scripts/``.

    Call once PER STACK: CDK refuses to bind one AssetCode instance in two
    stacks ("Asset is already associated with another stack"). The staging
    layer dedupes identical output hashes, so the upload happens once.
    The asset *path* is only the Docker-fallback mount and the SOURCE-hash
    root; keep it small (``src/``) -- the bundler copies from REPO_ROOT itself.
    """
    return lambda_.Code.from_asset(
        str(REPO_ROOT / "src"),
        asset_hash_type=AssetHashType.OUTPUT,
        bundling=BundlingOptions(image=RUNTIME.bundling_image, local=_CodeBundler()),
    )


def deps_layer(scope: Construct, construct_id: str) -> lambda_.LayerVersion:
    """The shared dependency layer. CUSTOM hash -> rebuilt only when deps change."""
    requirements = read_layer_requirements()
    return lambda_.LayerVersion(
        scope,
        construct_id,
        code=lambda_.Code.from_asset(
            str(REPO_ROOT / "infrastructure"),
            asset_hash_type=AssetHashType.CUSTOM,
            asset_hash=layer_asset_hash(requirements),
            bundling=BundlingOptions(
                image=RUNTIME.bundling_image, local=_LayerBundler(requirements)
            ),
        ),
        compatible_runtimes=[RUNTIME],
        compatible_architectures=[ARCHITECTURE],
        description=(
            "crypto-signals runtime dependencies (pyproject [project].dependencies minus "
            + ", ".join(sorted(LAYER_EXCLUDES))
            + "); manylinux2014_x86_64 / cp311 wheels."
        ),
    )


# ---------------------------------------------------------------------------
# CLI (used by CI's Trivy scan and for local inspection)
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lambda_assets",
        description="Build / inspect the Lambda code asset and dependency layer.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("print-requirements", help="print the filtered layer requirement specs")
    p_code = sub.add_parser("copy-code", help="copy src/ + scripts/ into --out")
    p_code.add_argument("--out", required=True, type=Path)
    p_layer = sub.add_parser("build-layer", help="pip-install the layer into --out/python")
    p_layer.add_argument("--out", required=True, type=Path)
    p_freeze = sub.add_parser(
        "freeze", help="build the layer in a temp dir and write `pip freeze` of it to --out"
    )
    p_freeze.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    requirements = read_layer_requirements()
    if args.command == "print-requirements":
        print("\n".join(requirements))
        return 0
    if args.command == "copy-code":
        args.out.mkdir(parents=True, exist_ok=True)
        copy_code(args.out)
        print(f"code asset: {_mb(dir_size(args.out))} -> {args.out}")
        return 0
    if args.command == "build-layer":
        args.out.mkdir(parents=True, exist_ok=True)
        build_layer(args.out, requirements)
        print(f"layer: {_mb(dir_size(args.out / 'python'))} -> {args.out}")
        return 0
    if args.command == "freeze":
        with tempfile.TemporaryDirectory() as tmp:
            build_layer(Path(tmp), requirements)
            frozen = subprocess.run(
                [sys.executable, "-m", "pip", "freeze", "--path", str(Path(tmp) / "python")],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(frozen, encoding="utf-8")
        print(f"wrote {len(frozen.splitlines())} pinned requirements -> {args.out}")
        return 0
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
