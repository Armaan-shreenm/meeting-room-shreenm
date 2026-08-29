"""Every operational script must at least import and compile.

This exists because a broken `scripts/seed.py` once reached three commits: the
test suite never imported it, and `start.sh` runs it on every deploy, so a
syntax error there fails the deploy rather than a test. The whole suite passing
while the deploy is dead is exactly the failure this prevents.
"""

from __future__ import annotations

import compileall
import importlib
import pathlib
import subprocess
import sys

import pytest

SCRIPTS = ["seed", "verify_constraint", "load_holidays", "daily_summary"]


@pytest.mark.parametrize("name", SCRIPTS)
def test_script_imports(name):
    """A syntax error or bad import fails here rather than on Render."""
    importlib.import_module(f"scripts.{name}")


def test_every_source_file_compiles():
    """compileall over the whole tree, so nothing ships unparseable."""
    root = pathlib.Path(__file__).resolve().parent.parent
    for package in ("app", "scripts", "alembic", "tests"):
        assert compileall.compile_dir(
            str(root / package), quiet=2, force=True
        ), f"{package} failed to compile"


def test_seed_is_idempotent_and_exits_zero():
    """start.sh runs this on every deploy; a non-zero exit fails the deploy."""
    result = subprocess.run(
        [sys.executable, "-m", "scripts.seed"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "Seed complete" in result.stdout + result.stderr


def test_load_holidays_parses_the_sample_csv():
    root = pathlib.Path(__file__).resolve().parent.parent
    sample = root / "holidays_sample.csv"
    assert sample.is_file(), "holidays_sample.csv is referenced by the README"

    result = subprocess.run(
        [sys.executable, "-m", "scripts.load_holidays", str(sample), "--dry-run"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "6 added" in result.stdout + result.stderr
