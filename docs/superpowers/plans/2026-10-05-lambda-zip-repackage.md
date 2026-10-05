# Lambda Zip Repackage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repackage the scan Lambda and the alarm-notifier Lambda from a container image into a zip code asset (console-editable) plus one shared dependency layer, with no Docker anywhere in the build or deploy path.

**Architecture:** A new `infrastructure/lambda_assets.py` module owns asset construction: pure functions filter the dependency list from `pyproject.toml`, copy `src/` + `scripts/` into a code asset, run pip with manylinux flags into a layer, and enforce size guards. CDK local-bundling wrappers turn those functions into `lambda_.Code` objects. `ComputeStack` builds the layer and the scan function; `MonitoringStack` builds its own code asset and reuses the layer by reference. The CI image job becomes a Trivy filesystem scan of the frozen layer requirements. Cutover is a one-time destroy/recreate of the three stateless stacks.

**Tech Stack:** Python 3.11, aws-cdk-lib 2.259.0 (`aws_lambda.Function`, `LayerVersion`, `Code.from_asset` with `BundlingOptions(local=...)`), jsii, `tomllib`, pip `--platform manylinux2014_x86_64 --only-binary=:all:`, pytest, cdk-nag, GitHub Actions, Trivy.

**Spec:** `docs/superpowers/specs/2026-10-03-lambda-zip-repackage-design.md`

## Global Constraints

- Runtime `python3.11`, architecture `x86_64`, for both functions (spec §3.1, §3.3).
- Scan handler string unchanged: `scripts.run_scan.lambda_handler`. Notifier handler: `scripts.alarm_notifier.lambda_handler`.
- Code asset contains only `src/**` and `scripts/**` (no `__pycache__`, no `*.pyc`); code asset must be ≤ 2.5 MB (console editor cap is 3 MB) (spec §3.1, §3.2).
- Layer must be ≤ 240 MB unzipped (hard cap 250 MB); built from `[project].dependencies` in `pyproject.toml` minus exactly `{"boto3", "psycopg"}` (spec §3.2).
- Pip flags: `--platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.11 --implementation cp` (spec §3.2).
- No Docker: local bundling only; a bundling failure must raise, never fall back to Docker (spec §3.2).
- Layer asset hash is CUSTOM, derived from the filtered requirement list + python version + platform, so the layer is rebuilt only when dependencies change (spec §3.2).
- Memory, timeout, env vars, IAM grants, explicit log groups: unchanged for both functions (spec §3.1, §3.3).
- `asyncpg` stays in the layer; `src/persistence/store.py` import is not touched (spec §2, §10).
- `src/common/llm.py`, model pin, prompts: untouched (spec §1 constraint 4, §10).
- Universal checkpoints before every commit: `ruff check .`, `ruff format --check .`, `mypy --strict src/ scripts/`, `pytest`, pre-commit, no secrets in diff.
- Branch: `feat/lambda-zip-repackage` (already created from `main`, carries the spec commit).

## Review Focus

1. **Dependency spec with an extra, a comma-separated specifier, or an environment marker** (`psycopg[binary]>=3.2`, `pydantic>=2.7,<3`, `foo>=1; python_version<"3.12"`): `requirement_name` must return the bare normalized name and the full spec must be passed to pip untouched. Pinned in Task 1 (`test_requirement_name_*`).
2. **`pyproject.toml` without `[project].dependencies`**: must raise a clear `ValueError`, not a `KeyError` from deep inside the bundler during `cdk synth`. Pinned in Task 1 (`test_layer_requirements_missing_section_is_a_clear_error`).
3. **pip failing (no manylinux wheel, network down)**: `build_layer` must raise with pip's output and `try_bundle` must not return `False` (which would make CDK try Docker). Pinned in Task 1 (`test_build_layer_propagates_pip_failure`) and Task 2 (bundler raises, not returns False).
4. **`__pycache__` nested several levels deep** in `src/` or inside a layer package: must be stripped everywhere, not only at the top level. Pinned in Task 1 (`test_copy_code_*`, `test_build_layer_strips_nested_pycache`).
5. **Same code asset bound in two stacks**: `AssetCode` raises "already associated with another stack" if one instance is reused across stacks; Monitoring must build its own instance. Pinned in Task 3 (both stacks synthesize in one app and both functions carry `Layers`).

---

### Task 1: `lambda_assets` module — pure asset builders + CLI

**Files:**
- Create: `infrastructure/lambda_assets.py`
- Test: `tests/infra/test_lambda_assets.py`

**Interfaces:**
- Consumes: nothing from other tasks. Reads `pyproject.toml` at the repo root.
- Produces (used by Tasks 2–4):
  - `REPO_ROOT: Path`
  - `LAYER_EXCLUDES: frozenset[str] = {"boto3", "psycopg"}`
  - `CODE_MAX_BYTES: int`, `LAYER_MAX_BYTES: int`
  - `class AssetTooLargeError(RuntimeError)`
  - `requirement_name(spec: str) -> str`
  - `layer_requirements(pyproject_text: str, exclude: frozenset[str] = LAYER_EXCLUDES) -> list[str]`
  - `layer_asset_hash(requirements: list[str]) -> str`
  - `pip_install_command(target: Path, requirements: list[str], *, python: str = sys.executable) -> list[str]`
  - `copy_code(output_dir: Path, *, repo_root: Path = REPO_ROOT) -> None`
  - `build_layer(output_dir: Path, requirements: list[str], *, runner: Callable[[list[str]], None] = run_pip) -> None`
  - `code_asset() -> lambda_.Code` (new `AssetCode` instance per call)
  - `deps_layer(scope: Construct, construct_id: str) -> lambda_.LayerVersion`
  - CLI: `python infrastructure/lambda_assets.py {print-requirements | copy-code --out DIR | build-layer --out DIR | freeze --out FILE}`

- [ ] **Step 1: Write the failing tests for the pure functions**

Create `tests/infra/test_lambda_assets.py`:

```python
"""Unit tests for infrastructure/lambda_assets.py (design 2026-10-03 §3.1-3.2).

The module is imported via tests/infra/conftest.py, which puts the
``infrastructure`` directory on sys.path (the same import root ``cdk synth``
uses). No pip, no network, no AWS: pip is injected as a fake runner.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import lambda_assets as la
import pytest

pytestmark = pytest.mark.filterwarnings("ignore")

PYPROJECT = """
[project]
name = "x"
dependencies = [
    "pydantic>=2.7,<3",
    "anthropic>=0.39",
    "boto3>=1.35",
    "ccxt>=4.4",
    "psycopg[binary]>=3.2",
    "aiohttp>=3.10",
]
"""


# ---------------------------------------------------------------------------
# requirement_name / layer_requirements
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("pydantic>=2.7,<3", "pydantic"),
        ("psycopg[binary]>=3.2", "psycopg"),
        ("aiohttp>=3.10", "aiohttp"),
        ('foo>=1; python_version<"3.12"', "foo"),
        ("  Boto3 >= 1.35", "boto3"),
        ("Some_Pkg.Name==1", "some-pkg-name"),
    ],
)
def test_requirement_name_returns_bare_normalized_name(spec: str, expected: str) -> None:
    assert la.requirement_name(spec) == expected


def test_layer_requirements_excludes_runtime_and_local_only_deps() -> None:
    reqs = la.layer_requirements(PYPROJECT)
    names = [la.requirement_name(r) for r in reqs]
    assert "boto3" not in names
    assert "psycopg" not in names
    # Everything else survives with its specifier intact, sorted by spec.
    assert reqs == sorted(["pydantic>=2.7,<3", "anthropic>=0.39", "ccxt>=4.4", "aiohttp>=3.10"])


def test_layer_requirements_exclusion_is_name_normalized() -> None:
    text = '[project]\ndependencies = ["Boto3>=1", "Psycopg[binary]>=3"]\n'
    assert la.layer_requirements(text) == []


def test_layer_requirements_missing_section_is_a_clear_error() -> None:
    with pytest.raises(ValueError, match=r"\[project\]\.dependencies"):
        la.layer_requirements("[project]\nname = 'x'\n")


# ---------------------------------------------------------------------------
# layer_asset_hash
# ---------------------------------------------------------------------------


def test_layer_asset_hash_is_stable_and_order_independent() -> None:
    a = la.layer_asset_hash(["aiohttp>=3.10", "ccxt>=4.4"])
    b = la.layer_asset_hash(["ccxt>=4.4", "aiohttp>=3.10"])
    assert a == b
    assert len(a) == 64  # sha256 hex


def test_layer_asset_hash_changes_when_requirements_change() -> None:
    assert la.layer_asset_hash(["ccxt>=4.4"]) != la.layer_asset_hash(["ccxt>=4.5"])


# ---------------------------------------------------------------------------
# pip_install_command
# ---------------------------------------------------------------------------


def test_pip_install_command_targets_the_lambda_platform(tmp_path: Path) -> None:
    cmd = la.pip_install_command(tmp_path, ["ccxt>=4.4"], python="py")
    assert cmd[:3] == ["py", "-m", "pip"]
    assert "install" in cmd
    assert "--target" in cmd and str(tmp_path / "python") in cmd
    assert "--platform" in cmd and "manylinux2014_x86_64" in cmd
    assert "--only-binary=:all:" in cmd
    assert "--python-version" in cmd and "3.11" in cmd
    assert "--implementation" in cmd and "cp" in cmd
    assert cmd[-1] == "ccxt>=4.4"


# ---------------------------------------------------------------------------
# copy_code
# ---------------------------------------------------------------------------


def _fake_repo(root: Path) -> None:
    (root / "src" / "agents" / "__pycache__").mkdir(parents=True)
    (root / "src" / "agents" / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"\x00")
    (root / "src" / "agents" / "a.py").write_text("A = 1\n")
    (root / "src" / "config").mkdir()
    (root / "src" / "config" / "strategies.yaml").write_text("x: 1\n")
    (root / "scripts").mkdir()
    (root / "scripts" / "run_scan.py").write_text("def lambda_handler(e, c): ...\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("")
    (root / "docs").mkdir()
    (root / "docs" / "d.md").write_text("")
    (root / "pyproject.toml").write_text(PYPROJECT)


def test_copy_code_copies_only_src_and_scripts_without_pycache(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _fake_repo(repo)
    out = tmp_path / "out"
    out.mkdir()

    la.copy_code(out, repo_root=repo)

    assert (out / "src" / "agents" / "a.py").read_text() == "A = 1\n"
    assert (out / "src" / "config" / "strategies.yaml").exists()
    assert (out / "scripts" / "run_scan.py").exists()
    assert not (out / "src" / "agents" / "__pycache__").exists()
    assert not (out / "tests").exists()
    assert not (out / "docs").exists()
    assert not (out / "pyproject.toml").exists()


def test_copy_code_rejects_oversized_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _fake_repo(repo)
    (repo / "src" / "big.bin").write_bytes(b"x" * 2048)
    monkeypatch.setattr(la, "CODE_MAX_BYTES", 1024)
    out = tmp_path / "out"
    out.mkdir()

    with pytest.raises(la.AssetTooLargeError, match="code asset"):
        la.copy_code(out, repo_root=repo)


# ---------------------------------------------------------------------------
# build_layer
# ---------------------------------------------------------------------------


def _fake_pip(files: dict[str, int]) -> Callable[[list[str]], None]:
    """Return a runner that 'installs' the given relative files (name -> size)."""

    def run(cmd: list[str]) -> None:
        target = Path(cmd[cmd.index("--target") + 1])
        for rel, size in files.items():
            p = target / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * size)

    return run


def test_build_layer_strips_nested_pycache(tmp_path: Path) -> None:
    out = tmp_path / "layer"
    out.mkdir()
    runner = _fake_pip(
        {
            "pkg/__init__.py": 10,
            "pkg/sub/__pycache__/m.cpython-311.pyc": 10,
            "pkg/sub/m.py": 10,
        }
    )

    la.build_layer(out, ["pkg>=1"], runner=runner)

    assert (out / "python" / "pkg" / "sub" / "m.py").exists()
    assert not (out / "python" / "pkg" / "sub" / "__pycache__").exists()


def test_build_layer_rejects_oversized_layer_and_names_top_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "layer"
    out.mkdir()
    monkeypatch.setattr(la, "LAYER_MAX_BYTES", 1000)
    runner = _fake_pip({"ccxt/__init__.py": 900, "tiny/__init__.py": 10, "mid/__init__.py": 200})

    with pytest.raises(la.AssetTooLargeError) as exc:
        la.build_layer(out, ["ccxt>=4.4"], runner=runner)

    msg = str(exc.value)
    assert "layer" in msg
    assert "ccxt" in msg  # the biggest offender is named
    assert msg.index("ccxt") < msg.index("mid") < msg.index("tiny")  # largest first


def test_build_layer_propagates_pip_failure(tmp_path: Path) -> None:
    out = tmp_path / "layer"
    out.mkdir()

    def failing(cmd: list[str]) -> None:
        raise subprocess.CalledProcessError(1, cmd, stderr="No matching distribution for foo")

    with pytest.raises(subprocess.CalledProcessError):
        la.build_layer(out, ["foo>=1"], runner=failing)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_print_requirements_prints_filtered_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _fake_repo(repo)
    monkeypatch.setattr(la, "REPO_ROOT", repo)

    assert la.main(["print-requirements"]) == 0

    lines = capsys.readouterr().out.strip().splitlines()
    assert "boto3>=1.35" not in lines
    assert "psycopg[binary]>=3.2" not in lines
    assert "ccxt>=4.4" in lines
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/infra/test_lambda_assets.py -q`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'lambda_assets'`.

- [ ] **Step 3: Implement `infrastructure/lambda_assets.py`**

```python
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


def layer_requirements(
    pyproject_text: str, exclude: frozenset[str] = LAYER_EXCLUDES
) -> list[str]:
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/infra/test_lambda_assets.py -q`
Expected: all PASS (18 tests: 6 parametrized name cases + 12 others).

- [ ] **Step 5: Smoke-run the real builders once (network, ~2 min) and record sizes**

Run from the repo root:

```powershell
.\.venv\Scripts\python.exe infrastructure\lambda_assets.py copy-code --out $env:TEMP\la-code
.\.venv\Scripts\python.exe infrastructure\lambda_assets.py build-layer --out $env:TEMP\la-layer
```

Expected: two lines like `code asset: 0.6 MB -> ...` and `layer: 19x.x MB -> ...` with no exception. If the layer line is above 240 MB the guard raised; stop and report (spec §4).

- [ ] **Step 6: Lint, format, commit**

```powershell
.\.venv\Scripts\python.exe -m ruff check . ; .\.venv\Scripts\python.exe -m ruff format .
git add infrastructure/lambda_assets.py tests/infra/test_lambda_assets.py
git commit -m "feat(infra): lambda_assets builders for the zip code asset + dependency layer (no Docker, size-guarded)"
```

---

### Task 2: ComputeStack → zip function + layer; synth tests skip bundling

**Files:**
- Modify: `infrastructure/stacks/compute_stack.py` (docstring lines 1-26, 55-58, 93-122, 165-190)
- Modify: `tests/infra/conftest.py:39` (App context)
- Modify: `tests/infra/test_stacks.py` (add compute packaging tests after line 80)

**Interfaces:**
- Consumes: `lambda_assets.code_asset()`, `lambda_assets.deps_layer(scope, id)`, `lambda_assets.RUNTIME`, `lambda_assets.ARCHITECTURE` (Task 1).
- Produces: `ComputeStack.function: lambda_.Function` (unchanged attribute name), `ComputeStack.deps_layer: lambda_.LayerVersion` (new; consumed by Task 3 via `app.py`), `ComputeStack.log_group` (unchanged).

- [ ] **Step 1: Write the failing synth tests**

Append to `tests/infra/test_stacks.py` after `test_compute_role_can_get_ssm_parameters`:

```python
# ---------------------------------------------------------------------------
# ComputeStack: zip packaging + dependency layer (design 2026-10-03 §3.1-3.2)
# ---------------------------------------------------------------------------


def test_compute_lambda_is_a_zip_function_on_python311(templates: dict[str, Any]) -> None:
    templates["compute"].resource_count_is("AWS::Lambda::Function", 1)
    templates["compute"].has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.11",
            "Handler": "scripts.run_scan.lambda_handler",
            "Architectures": ["x86_64"],
            "PackageType": assertions.Match.absent(),
            "Layers": assertions.Match.array_with([assertions.Match.any_value()]),
            "MemorySize": 1024,
            "Timeout": 600,
        },
    )


def test_compute_stack_owns_the_dependency_layer(templates: dict[str, Any]) -> None:
    templates["compute"].resource_count_is("AWS::Lambda::LayerVersion", 1)
    templates["compute"].has_resource_properties(
        "AWS::Lambda::LayerVersion",
        {
            "CompatibleRuntimes": ["python3.11"],
            "CompatibleArchitectures": ["x86_64"],
        },
    )
```

- [ ] **Step 2: Run to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/infra/test_stacks.py -q -k "zip or dependency_layer"`
Expected: FAIL (`PackageType: Image` present, 0 LayerVersion resources).

- [ ] **Step 3: Make the synth fixture skip bundling**

In `tests/infra/conftest.py` replace line 39 `app = core.App()` with:

```python
    # Skip asset bundling in unit tests: the code/layer bundlers would otherwise
    # copy src/ and pip-install ~190 MB of wheels on every test run. With an
    # empty bundling-stacks list CDK stages a placeholder and still renders the
    # Function/LayerVersion resources we assert on. The real bundling path is
    # exercised by `python app.py` (cdk synth) in CI's quality job.
    app = core.App(context={"aws:cdk:bundling-stacks": []})
```

- [ ] **Step 4: Rewrite the function in `compute_stack.py`**

Replace the module docstring lines 1-26 with:

```python
"""ComputeStack: the scan Lambda (zip + dependency layer) and its least-privilege role.

Implemented in Step 1.18 as a container image; repackaged per the 2026-10-03 design
(docs/superpowers/specs/2026-10-03-lambda-zip-repackage-design.md) so the source is
readable and editable in the Lambda console:

- A **zip-packaged Lambda** whose code asset is exactly ``src/`` + ``scripts/``
  (~0.6 MB, under the console editor's 3 MB cap) plus one **dependency layer**
  (~190 MB unzipped) built by ``infrastructure/lambda_assets.py`` with pip's
  manylinux flags -- no Docker anywhere.
- The function runs **outside any VPC**, so its egress to the non-AWS APIs it
  calls (Binance / Anthropic / Telegram) is free over the public internet and
  Aurora is reached over the RDS Data API (HTTPS) -- no VPC attachment, no NAT.
- A **least-privilege execution role** (NFR-3.2): RDS Data API access to the one
  Aurora cluster, read-only ``ssm:GetParameter`` on the two SSM SecureString
  parameters it uses (Anthropic / Telegram), read-write to a single S3 prefix,
  and CloudWatch Logs write -- nothing more.
- Non-secret config is injected via environment variables. Secret *values* are
  NOT baked into the template; the function is given the SSM parameter *names*
  and reads the values from Parameter Store at runtime (src/config/secrets.py).

Operator note: edits made in the console are REPLACED by the next ``cdk deploy``
(manual or the push-to-main auto-deploy). Git is the source of truth for deploys;
the console is the scratchpad. See docs/operations.md §4.1.

The layer is exposed as ``self.deps_layer`` so the MonitoringStack's notifier
Lambda can share it (the notifier builds its OWN code asset -- CDK refuses to bind
one AssetCode in two stacks).
"""
```

Replace lines 55-58 (the `REPO_ROOT` comment + constant) with:

```python
from lambda_assets import ARCHITECTURE, RUNTIME, code_asset, deps_layer
```

(and delete `from pathlib import Path` if no longer used — it is not).

Replace lines 93-122 (the `DockerImageFunction` block) with:

```python
        # ---- Dependency layer (shared with the MonitoringStack notifier) -----
        self.deps_layer = deps_layer(self, "DepsLayer")

        # ---- The scan Lambda (zip code asset + layer) -------------------------
        self.function = lambda_.Function(
            self,
            "ScanLambda",
            runtime=RUNTIME,
            architecture=ARCHITECTURE,
            handler="scripts.run_scan.lambda_handler",
            code=code_asset(),
            layers=[self.deps_layer],
            memory_size=1024,
            # One scan finishes in well under 5 min (NFR-4.1); 10 min leaves
            # headroom for a multi-symbol watchlist run, still under the 15 cap.
            timeout=Duration.minutes(10),
            log_group=self.log_group,
            environment={
                "PERSISTENCE_BACKEND": "dataapi",
                "DB_CLUSTER_ARN": cluster.cluster_arn,
                "DB_SECRET_ARN": cluster.secret.secret_arn,
                "DB_NAME": db_name,
                "BLOB_BUCKET": bucket.bucket_name,
                # SSM parameter NAMES (not values): the app reads the values at
                # runtime via ssm:GetParameter (src/config/secrets.py).
                ANTHROPIC_PARAM_ENV: ANTHROPIC_PARAM_NAME,
                TELEGRAM_PARAM_ENV: TELEGRAM_PARAM_NAME,
                "LOG_LEVEL": "INFO",
            },
            description=(
                "Crypto-signals scan: runs one scheduled SMC scan per invocation "
                "(signal-only). Invoked by EventBridge Scheduler. Zip-packaged so the "
                "source is editable in the console; deps live in the DepsLayer."
            ),
        )
```

Also update the class docstring on line 66 to `"""The scan Lambda (zip + layer) + its least-privilege execution role."""`.

In `_apply_nag_suppressions`, add a third entry to the list (after the IAM5 entry):

```python
                {
                    "id": "AwsSolutions-L1",
                    "reason": (
                        "python3.11 is deliberate: the project toolchain, tests, mypy and the "
                        "layer's manylinux wheels are pinned to 3.11 (pyproject requires-python, "
                        "CI matrix). A runtime bump is its own step, re-validating the wheel set."
                    ),
                },
```

- [ ] **Step 5: Run the compute tests and the whole infra suite**

Run: `.\.venv\Scripts\python.exe -m pytest tests/infra -q`
Expected: all PASS (existing SSM/env tests still pass; the two new ones pass; monitoring still synthesises because its `DockerImageFunction` is untouched until Task 3).

- [ ] **Step 6: Lint, format, commit**

```powershell
.\.venv\Scripts\python.exe -m ruff check . ; .\.venv\Scripts\python.exe -m ruff format .
git add infrastructure/stacks/compute_stack.py tests/infra/conftest.py tests/infra/test_stacks.py
git commit -m "feat(infra): scan Lambda as zip code asset + DepsLayer (console-editable); synth tests skip bundling"
```

---

### Task 3: MonitoringStack notifier → zip, sharing the layer; app wiring

**Files:**
- Modify: `infrastructure/stacks/monitoring_stack.py` (docstring lines 22-25, 56-58, 76-86, 95, 121-153, 266-279)
- Modify: `infrastructure/app.py:67-74`
- Modify: `tests/infra/conftest.py:48-54`
- Modify: `tests/infra/test_stacks.py` (replace `test_monitoring_has_sns_topic_and_lambda_subscription`, add a notifier packaging test)
- Modify: `scripts/alarm_notifier.py:8-10` (docstring only)

**Interfaces:**
- Consumes: `ComputeStack.deps_layer` (Task 2), `lambda_assets.code_asset()`, `RUNTIME`, `ARCHITECTURE` (Task 1).
- Produces: `MonitoringStack.__init__(..., deps_layer: lambda_.ILayerVersion, ...)` keyword (new, required).

- [ ] **Step 1: Write the failing tests**

In `tests/infra/test_stacks.py`, replace `test_monitoring_has_sns_topic_and_lambda_subscription` with:

```python
def test_monitoring_has_sns_topic_and_lambda_subscription(templates: dict[str, Any]) -> None:
    templates["monitoring"].resource_count_is("AWS::SNS::Topic", 1)
    templates["monitoring"].resource_count_is("AWS::Lambda::Function", 1)
    templates["monitoring"].has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "lambda"}
    )


def test_monitoring_notifier_is_a_zip_function_sharing_the_layer(
    templates: dict[str, Any],
) -> None:
    # Design 2026-10-03 §3.3: same zip code asset shape as the scan Lambda,
    # handler overridden, layer IMPORTED from Compute (no LayerVersion here).
    templates["monitoring"].resource_count_is("AWS::Lambda::LayerVersion", 0)
    templates["monitoring"].has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.11",
            "Handler": "scripts.alarm_notifier.lambda_handler",
            "Architectures": ["x86_64"],
            "PackageType": assertions.Match.absent(),
            "Layers": assertions.Match.array_with([assertions.Match.any_value()]),
            "MemorySize": 256,
            "Timeout": 30,
        },
    )
```

- [ ] **Step 2: Run to verify the new test fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/infra/test_stacks.py -q -k notifier_is_a_zip`
Expected: FAIL (`PackageType: Image`, no `Layers`).

- [ ] **Step 3: Update the fixture and `app.py` to pass the layer**

`tests/infra/conftest.py` lines 48-54 become:

```python
    monitoring = MonitoringStack(
        app,
        "Monitoring",
        scan_function=compute.function,
        scan_log_group=compute.log_group,
        deps_layer=compute.deps_layer,
        cluster=data.cluster,
    )
```

`infrastructure/app.py` lines 67-74 become:

```python
MonitoringStack(
    app,
    "CryptoSignals-Monitoring",
    env=env,
    scan_function=compute.function,
    scan_log_group=compute.log_group,
    deps_layer=compute.deps_layer,
    cluster=data.cluster,
)
```

- [ ] **Step 4: Rewrite the notifier in `monitoring_stack.py`**

Docstring lines 22-25 become:

```
The notifier Lambda ships as a zip of the same ``src/`` + ``scripts/`` code asset
as the scan Lambda (handler overridden to ``scripts.alarm_notifier.lambda_handler``)
and attaches the scan's dependency layer, so there is one artifact set to build and
patch (design 2026-10-03 §3.3). It reads the Telegram token/chat from the same SSM
SecureString parameter the scan uses.
```

Delete lines 56-58 (`REPO_ROOT` comment + constant) and the now-unused `from pathlib import Path`; add after the `stacks.parameters` import:

```python
from lambda_assets import ARCHITECTURE, RUNTIME, code_asset
```

Constructor signature (lines 76-86) gains the keyword:

```python
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        scan_function: lambda_.IFunction,
        scan_log_group: logs.ILogGroup,
        deps_layer: lambda_.ILayerVersion,
        cluster: rds.DatabaseCluster,
        **kwargs: Any,
    ) -> None:
```

Line 95 becomes `self.notifier = self._build_notifier(deps_layer)`.

`_build_notifier` (lines 121-153) becomes:

```python
    def _build_notifier(self, deps_layer: lambda_.ILayerVersion) -> lambda_.Function:
        """A small Lambda that posts CloudWatch alarms to Telegram.

        Same zip code asset as the scan Lambda (a FRESH AssetCode instance -- CDK
        refuses to bind one instance in two stacks; staging dedupes the upload),
        handler overridden, scan dependency layer attached by reference.
        """
        log_group = logs.LogGroup(
            self,
            "AlarmNotifierLogs",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )
        notifier = lambda_.Function(
            self,
            "AlarmNotifier",
            runtime=RUNTIME,
            architecture=ARCHITECTURE,
            handler="scripts.alarm_notifier.lambda_handler",
            code=code_asset(),
            layers=[deps_layer],
            memory_size=256,
            timeout=Duration.seconds(30),
            log_group=log_group,
            environment={
                TELEGRAM_PARAM_ENV: TELEGRAM_PARAM_NAME,
                "LOG_LEVEL": "INFO",
            },
            description=(
                "Posts CloudWatch alarm state changes to the operator's Telegram "
                "bot (subscribed to the alarm SNS topic)."
            ),
        )
```

(the `add_to_role_policy` block and `return notifier` stay as they are.)

In `_apply_nag_suppressions`, the notifier suppression list (lines 268-277) gains:

```python
                {
                    "id": "AwsSolutions-L1",
                    "reason": (
                        "python3.11 is deliberate and matches the scan Lambda and the shared "
                        "dependency layer's cp311 wheels; a runtime bump is its own step."
                    ),
                },
```

`scripts/alarm_notifier.py` lines 8-10 become:

```
Ships in the same zip code asset + dependency layer as the scan Lambda (handler
overridden to this module) so there is one artifact set to build/patch. It reads
the Telegram token + chat id from the same SSM SecureString parameter the scan
Lambda uses (``TELEGRAM_PARAM_NAME``), via the
```

- [ ] **Step 5: Run the full infra suite and the whole test suite**

Run: `.\.venv\Scripts\python.exe -m pytest -q`
Expected: all PASS. The monitoring synth proves both stacks can carry their own `AssetCode` in one app (Review Focus 5).

- [ ] **Step 6: Lint, format, type-check, commit**

```powershell
.\.venv\Scripts\python.exe -m ruff check . ; .\.venv\Scripts\python.exe -m ruff format .
.\.venv\Scripts\python.exe -m mypy --strict src/ scripts/
git add infrastructure/stacks/monitoring_stack.py infrastructure/app.py tests/infra/conftest.py tests/infra/test_stacks.py scripts/alarm_notifier.py
git commit -m "feat(infra): alarm-notifier Lambda as zip sharing the DepsLayer; app wiring"
```

---

### Task 4: Remove the Dockerfile; CI dependency scan + synth; CD comments

**Files:**
- Delete: `Dockerfile.lambda`
- Modify: `.github/workflows/ci.yml` (header lines 1-11; add a synth step in `quality`; replace the `image` job lines 72-94)
- Modify: `.github/workflows/deploy-dev.yml` (lines 6-8, 116-145)
- Modify: `.github/workflows/deploy-prod.yml` (line 8)

**Interfaces:**
- Consumes: CLI `python infrastructure/lambda_assets.py freeze --out <file>` (Task 1); `python app.py` synth (Tasks 2-3).
- Produces: nothing code-level.

- [ ] **Step 1: Delete the Dockerfile and confirm nothing references it**

```powershell
git rm Dockerfile.lambda
```

Run: `rg -n "Dockerfile\.lambda|DockerImageFunction|DockerImageCode" --glob "!docs/**" --glob "!*.md"`
Expected: no matches outside docs (docs are handled in Task 5).

- [ ] **Step 2: Edit `ci.yml`**

Header comment (lines 1-11) becomes:

```yaml
# Continuous integration (Slice 1 Step 1.20; repackaged 2026-10-03).
#
# Two jobs:
#   quality   - lint, format, type-check, test, and a full `cdk synth` (which runs
#               the zip/layer bundlers + cdk-nag + the asset size guards). Runs on
#               every push and PR.
#   deps-scan - build the Lambda dependency layer, pin it with `pip freeze`, and
#               scan the pinned set with Trivy. Runs only on a push to main (after
#               quality passes), replacing the former container-image scan.
#
# Lint/format run through pre-commit so CI uses the EXACT pinned tool versions
# from .pre-commit-config.yaml -- no drift between "passes locally" and "passes
# in CI". mypy is skipped in that step and run once explicitly (covering
# src/ + scripts/) so it isn't executed twice with two mypy versions.
```

In the `quality` job, add after the `Tests + coverage` step (before `Upload coverage report`):

```yaml
      - name: CDK synth (bundlers + cdk-nag + size guards)
        # Builds the real code asset and dependency layer with pip (no Docker)
        # and fails on any cdk-nag finding or asset-size-guard breach.
        working-directory: infrastructure
        env:
          JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION: "1"
        run: python app.py
```

Replace the `image` job (lines 72-94) with:

```yaml
  deps-scan:
    name: Build dependency layer + Trivy scan
    needs: quality
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip

      - name: Install CDK app deps (lambda_assets imports aws_cdk)
        run: pip install -r infrastructure/requirements.txt

      - name: Build the layer and pin it
        # Installs the exact manylinux wheel set the Lambda layer ships, then
        # `pip freeze`s it so Trivy sees pinned versions (it skips `>=` floors).
        run: python infrastructure/lambda_assets.py freeze --out layer-scan/requirements.txt

      - name: Scan pinned dependencies with Trivy
        # Pin to a current release: older trivy-action tags (e.g. v0.28.0)
        # internally reference aquasecurity/setup-trivy@v0.2.1, whose tag Aqua
        # has since DELETED, so they fail to resolve. v0.36.0 SHA-pins
        # setup-trivy@v0.2.6, making it immune to that retagging.
        uses: aquasecurity/trivy-action@v0.36.0
        with:
          scan-type: fs
          scan-ref: layer-scan
          format: table
          severity: CRITICAL,HIGH
          ignore-unfixed: true
          exit-code: "1"
```

- [ ] **Step 3: Edit `deploy-dev.yml`**

Lines 6-8 become:

```yaml
# Why no docker / ECR step: ComputeStack and MonitoringStack package the Lambdas
# as a zip code asset + a dependency layer built by infrastructure/lambda_assets.py
# with pip's manylinux flags, so `cdk deploy` needs only python + pip (no Docker).
```

Replace the `Deploy all stacks` step (lines 116-145) with:

```yaml
      - name: Deploy all stacks
        working-directory: infrastructure
        env:
          JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION: "1"
        run: cdk deploy --all --require-approval never
```

(The Step 2.12 "Compute-first" transition is complete: the 2026-10-03 cutover recreates Compute/Scheduling/Monitoring, so there are no stale exports left to release.)

- [ ] **Step 4: Edit `deploy-prod.yml` line 8**

```yaml
# As with dev, `cdk deploy` builds the zip code asset + dependency layer itself
# (python + pip, no Docker), and the DB schema migration runs automatically
```

(keep the following line `# idempotent; ARNs discovered ...` intact.)

- [ ] **Step 5: Validate YAML and run the local equivalent of the synth step**

```powershell
.\.venv\Scripts\python.exe -m pre_commit run check-yaml --all-files
cd infrastructure; $env:JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION="1"; ..\.venv\Scripts\python.exe app.py; cd ..
```

Expected: `check-yaml` passes; `app.py` completes with no cdk-nag errors (warnings are fine) and `infrastructure/cdk.out/` contains two `asset.*` directories (code, layer) — the layer build takes ~1-2 minutes on the first run and is skipped on the second run because the CUSTOM hash already exists in `cdk.out`.

- [ ] **Step 6: Commit**

```powershell
git add -A .github/workflows Dockerfile.lambda
git commit -m "ci: drop the container image; Trivy fs scan of the pinned layer + cdk synth gate; CD no longer needs Docker"
```

---

### Task 5: Documentation + SPEC amendments

**Files:**
- Modify: `SPEC.md:139-140, 167, 515-516, 538-539, 545-546`
- Modify: `CLAUDE.md:58, 104`
- Modify: `docs/PROJECT_STATE.md:32, 51, 108, 141-146`
- Modify: `docs/operations.md:104-111, 150, 188-204`
- Modify: `docs/memory-snapshot/project_serverless_pivot.md` (append one line)

**Interfaces:** none.

- [ ] **Step 1: SPEC.md amendments (spec §9)**

Lines 139-140 become:

```
| Agent compute | AWS Lambda (zip + dependency layer) | Event-driven, scale-to-zero, no cluster to manage; one scan finishes in seconds. Zip packaging keeps the source readable/editable in the Lambda console (2026-10-03 repackage; was a container image) |
| Packaging | Zip code asset (`src/` + `scripts/`, ~0.6 MB, <3 MB console-editor cap) + one Lambda Layer (~190 MB unzipped of 250 MB cap) built by `infrastructure/lambda_assets.py` with pip manylinux wheels — **no Docker** | The earlier "deps too large for a zip" held for a single zip; with a layer the measured set fits. Size guards fail synth before either cap |
```

Line 167 becomes:

```
| Dependency scanning | Trivy `fs` over the pinned layer requirement set (in CI) | Catches vulnerable Python dependencies; replaced the container-image scan when the image was dropped (2026-10-03) |
```

Lines 515-516 become:

```
- Build the scan **Lambda as a zip code asset + dependency layer** (originally a
  container image; repackaged 2026-10-03 for console editability, see
  `docs/superpowers/specs/2026-10-03-lambda-zip-repackage-design.md`)
```

Lines 538-539 become:

```
- On every push to main: run full test suite, build the Lambda dependency layer,
  scan its pinned requirements with Trivy
```

Lines 545-546 become:

```
  - `cdk deploy` synthesises the zip code asset + dependency layer (no Docker,
    no ECR push)
```

- [ ] **Step 2: CLAUDE.md**

Line 58 becomes:

```
| Compute | **AWS Lambda** (zip code asset + dependency layer, outside VPC, **source editable in the console**) for the agent pipeline — revised from ECS Fargate, repackaged from a container image 2026-10-03, see SPEC §2.4. Dashboard (Slice 4) is a separate long-running Fargate/App Runner service. |
```

Line 104: change `(container image, **ap-south-1**)` to `(zip + layer, **ap-south-1**)`.

- [ ] **Step 3: PROJECT_STATE.md**

Line 32: `AWS Lambda (container image, **outside any VPC**)` → `AWS Lambda (zip + dependency layer, console-editable, **outside any VPC**)`.

Line 51: `→ Lambda (container, ap-south-1)` → `→ Lambda (zip + layer, ap-south-1)`.

Line 108: `Prereqs: **Python 3.11+, Docker Desktop, Node 20+ (for the CDK CLI), AWS CLI v2, git.**` → `Prereqs: **Python 3.11+, Node 20+ (for the CDK CLI), AWS CLI v2, git.** (Docker Desktop only for the local docker-compose Postgres; deploys no longer need it.)`

Lines 141-146 become:

```bash
# Deploy (no Docker: cdk builds the zip code asset + dependency layer with pip)
cd infrastructure
export PATH="/<drive>/.../Trading Multi Agent/.venv/Scripts:$PATH"   # so `python app.py` finds aws-cdk-lib
export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1                  # silence Node-version banner
cdk deploy --all --require-approval never
#   ^ first synth pip-installs the ~190 MB layer (1-2 min); later synths reuse it
#     from cdk.out until pyproject dependencies change. Code-only deploys ship 0.6 MB.
#   ^ `cdk deploy --hotswap CryptoSignals-Compute` pushes a code-only change in seconds.
```

Add to §3 live-facts table a row after "Lambda log group":

```
| **Console editing** | The scan + notifier functions are zip-packaged: open **Code** in the Lambda console to read/edit `src/` and `scripts/`, Deploy, then Test (`docs/operations.md §4.1`). **Any `cdk deploy` overwrites console edits.** |
```

- [ ] **Step 4: operations.md**

Lines 104-111 become:

```
CD deploys on merge to `main` (dev) and on a `v*.*.*` tag (prod, with a manual
approval gate). To deploy by hand from the repo root (no Docker needed — CDK
builds the zip code asset and the dependency layer with pip):

```bash
cd infrastructure
cdk deploy --all --region ap-south-1
```

If synth fails with `AssetTooLargeError`, a dependency pushed the layer over the
240 MB guard (or the code over 2.5 MB); the message names the largest packages.
Trim the dependency or move it to an optional extra — do not raise the guard.
```

Line 150: `(`AlarmNotifier`, reuses the scan image)` → `(`AlarmNotifier`, same zip code asset + layer as the scan Lambda)`.

Insert a new subsection before `## 4. Routine operations` code block (i.e. right after the `## 4. Routine operations` heading, as §4.1), then keep the existing CLI block as §4.2:

```
### 4.1 Editing and testing in the Lambda console

Both Lambdas are zip-packaged, so the console shows the full `src/` and
`scripts/` tree (design 2026-10-03). Workflow:

1. Lambda console → the scan function → **Code** tab. Edit any file (prompts,
   `src/config/strategies.yaml`, risk gates, pipeline logic, extra logging).
2. **Deploy** (console button, a few seconds).
3. **Test** tab. Useful saved events:

   | Event | What it runs |
   |---|---|
   | `{}` | full watchlist scan |
   | `{"symbols": ["BTCUSDT"]}` | one symbol |
   | `{"mode": "forecaster"}` | Forecaster sweep over open setups |
   | `{"mode": "resolve", "chunk_size": 25}` | Critic v0 outcome resolver |
   | `{"mode": "migrate"}` | apply the DB schema over the Data API |

   The JSON summary appears inline; logs under **Monitor → View CloudWatch logs**.
   First call after idle may hit Aurora resuming — retry after ~8 s.

**Caveats**
- **Every `cdk deploy` (manual, or the push-to-main auto-deploy) replaces the
  function code with the git contents.** Copy anything you want to keep into the
  repo by hand first. Git is the source of truth for deploys; the console is a
  scratchpad.
- A new third-party import that is not in the dependency layer fails at init
  (`ImportError`) and trips `ScanFailureRateAlarm` → Telegram. Add it to
  `pyproject.toml` and deploy from git instead.
- A syntax error behaves the same way; fix it in the console or redeploy.

### 4.2 CLI equivalents
```

- [ ] **Step 5: memory snapshot**

Append to `docs/memory-snapshot/project_serverless_pivot.md`:

```
- 2026-10-03: Lambda repackaged from container image → zip code asset (`src/`+`scripts/`) + dependency layer, console-editable; no Docker in build/deploy. Spec `docs/superpowers/specs/2026-10-03-lambda-zip-repackage-design.md`.
```

- [ ] **Step 6: Verify no stale references remain, commit**

Run: `rg -n "container image|Dockerfile\.lambda|reuses the scan image|Docker Desktop must be running" SPEC.md CLAUDE.md docs scripts infrastructure .github`
Expected: only historical mentions that say "was a container image" / "originally a container image"; no instructions that still require Docker.

```powershell
.\.venv\Scripts\python.exe -m pre_commit run --all-files
git add SPEC.md CLAUDE.md docs/PROJECT_STATE.md docs/operations.md docs/memory-snapshot/project_serverless_pivot.md
git commit -m "docs: zip + layer packaging, console editing runbook, SPEC §2.4/§2.6/1.18/1.20 amendments"
```

---

### Task 6: Cutover + live verification (GitHub Actions only; destructive on three stateless stacks)

> **Hard rule (operator, 2026-10-05): this machine has no AWS CLI, no CDK CLI and no AWS
> credentials, by design. Every AWS operation runs in GitHub Actions.** The original
> laptop-side steps were replaced by a `workflow_dispatch` input on `deploy-dev.yml`
> (`recreate_stateless_stacks`), added in the review fix pass.

**Files:**
- Modify (after deploy): `docs/PROJECT_STATE.md:89, 93` (new function name + log group), §2 status paragraph.

**Interfaces:** `.github/workflows/deploy-dev.yml` `workflow_dispatch` input `recreate_stateless_stacks: boolean` → step "Recreate stateless stacks (manual cutover only)" runs `cdk destroy CryptoSignals-Monitoring CryptoSignals-Scheduling CryptoSignals-Compute --force` before `cdk deploy --all`.

> The manual run deletes the Compute, Scheduling and Monitoring stacks and recreates them. It does **not** touch `CryptoSignals-Data` (Aurora, S3, DB secret), `CryptoSignals-Network`, or the SSM parameters. CloudWatch log history for the two functions (2-week retention) is lost. Spec §6 / §11.

- [ ] **Step 1: Merge the PR (`feat/lambda-zip-repackage` → `main`)**

Expected: CI green; then the **automatic** Deploy (dev) run goes **red** on `CryptoSignals-Compute` with "Cannot update export ... in use by CryptoSignals-Scheduling/Monitoring" (CloudFormation rolls back; nothing changes). This one red run is accepted (operator decision 2026-10-05).

- [ ] **Step 2: Operator triggers the cutover from the GitHub UI**

GitHub → Actions → **Deploy (dev)** → **Run workflow** → branch `main` → tick **recreate_stateless_stacks** → Run.

Expected in the log: the "Recreate stateless stacks" step prints three `destroyed` lines, then `cdk deploy --all` reports Network and Data `(no changes)` and creates Compute, Scheduling, Monitoring. The `ScanFunctionName` and `AlarmNotifierName` outputs are printed at the end of the Compute / Monitoring deploys.

- [ ] **Step 3: Operator pastes the outputs into chat**

`ScanFunctionName`, `AlarmNotifierName`, and the scan log group name (Lambda console → the scan function → Monitor → the CloudWatch log group link). The assistant cannot query AWS.

- [ ] **Step 4: Verify in the console (the whole point of the change)**

1. Lambda console → the new scan function → **Code** tab: the `src/` and `scripts/` tree is visible and editable; the editor does NOT say "The deployment package of your Lambda function is too large to enable inline code editing".
2. **Test** with each event from `docs/operations.md §4.1`:
   - `{}` → `{"ok": true, ...}` and a Telegram message.
   - `{"symbols": ["BTCUSDT"]}` → `{"ok": true, ...}`.
   - `{"mode": "forecaster"}` → `{"ok": true, "mode": "forecaster", ...}`.
   - `{"mode": "resolve", "chunk_size": 25}` → `{"ok": true, "mode": "resolve", ...}` — **skip on this branch**: the resolver lives on `feat/slice-2-step-2.14-critic-v0`; here an unknown mode falls through to a full scan (real Telegram + LLM spend).
   - `{"mode": "migrate"}` → `{"ok": true, "mode": "migrate", "statements": N}`.
3. Make a trivial console edit (add one `logger.info("console-edit smoke")` to `scripts/run_scan.py` → `lambda_handler`), Deploy, Test `{}` again, and confirm the line appears in CloudWatch logs. Then remove it and Deploy again.
4. Notifier (no CLI): SNS console → Topics → the `crypto-signals alarms` topic → **Publish message** → body `{"AlarmName":"cutover-test","NewStateValue":"ALARM","NewStateReason":"runbook"}` → Telegram message arrives.
5. EventBridge Scheduler console: 5 schedules present and ENABLED (4 scan windows + forecaster sweep; the `rate(2 days)` resolver schedule arrives with the Critic v0 merge).

- [ ] **Step 5: Record the new live IDs**

Update `docs/PROJECT_STATE.md` from the values the operator pasted in Step 3:
- line 89 `**Lambda function**` → the new `ScanFunctionName` output.
- line 93 `**Lambda log group**` → the new log group name.
- §2 status: add one sentence `**Repackaged 2026-10-0X:** zip + layer, console editing verified (scan/forecaster/resolve/migrate events + notifier).`

```powershell
git add docs/PROJECT_STATE.md
git commit -m "docs(state): live IDs after the zip + layer cutover; console editing verified"
```

- [ ] **Step 6: Finish the branch**

(The PR is opened BEFORE Step 1 of this task via `superpowers:finishing-a-development-branch`; this step is the post-cutover wrap-up.) Note in the PR body: (a) the expected red automatic deploy and the manual `recreate_stateless_stacks` run that follows; (b) the branch does not include the unmerged Critic v0 work, and `infrastructure/stacks/*.py` / `tests/infra/test_stacks.py` will need a small merge with `feat/slice-2-step-2.14-critic-v0` (which adds the `rate(2 days)` schedule and its tests; no overlap with the packaging changes).
