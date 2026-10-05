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
