"""End-to-end browser tests of the dashboard UI (Playwright via node).

Runs ``tests/e2e/ui_smoke.cjs`` (every view including the Simulation view, the drawer, themes, outages,
phone layout, hostile data) and ``tests/e2e/regressions.cjs`` (one check per UI bug fixed after QA, named
after its id, plus the Simulation view's states on ``page.route()`` fixtures)
against ``tests/e2e/serve_demo.py`` (the simulated market: no API key, no network). Both are
skipped when node or the Playwright package is not installed. Screenshots go to a temporary
folder here; run ``node tests/e2e/ui_smoke.cjs`` directly to refresh ``docs/screenshots/``.
Deselect with ``pytest -m "not e2e"``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "tests" / "e2e" / "ui_smoke.cjs"
REGRESSIONS = ROOT / "tests" / "e2e" / "regressions.cjs"
DEFAULT_PLAYWRIGHT = "/opt/node-tools/node_modules/playwright"


def _playwright_module() -> str:
    return os.environ.get("PLAYWRIGHT_MODULE", DEFAULT_PLAYWRIGHT)


def _have_playwright(node: str) -> bool:
    module = _playwright_module()
    try:
        done = subprocess.run(
            [node, "-e", "require(process.argv[1])", module],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    if not _have_playwright(node):
        pytest.skip(f"the playwright package is not available at {_playwright_module()}")
    return node


@pytest.mark.e2e
def test_dashboard_ui_regressions() -> None:
    node = _node()
    env = dict(os.environ)
    env.setdefault("PYTHON", os.environ.get("PYTHON") or shutil.which("python3") or "python3")
    env["PLAYWRIGHT_MODULE"] = _playwright_module()
    done = subprocess.run(
        [node, str(REGRESSIONS)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    output = (done.stdout or "") + (done.stderr or "")
    assert done.returncode == 0, "UI regression checks failed:\n" + output[-8000:]
    assert "all regression checks passed" in done.stdout, output[-8000:]


@pytest.mark.e2e
def test_dashboard_ui_smoke(tmp_path: Path) -> None:
    node = _node()
    env = dict(os.environ)
    env.setdefault("PYTHON", os.environ.get("PYTHON") or shutil.which("python3") or "python3")
    env["E2E_SCREENSHOT_DIR"] = os.environ.get("E2E_SCREENSHOT_DIR") or str(tmp_path / "screenshots")
    env["PLAYWRIGHT_MODULE"] = _playwright_module()
    done = subprocess.run(
        [node, str(SMOKE)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    output = (done.stdout or "") + (done.stderr or "")
    assert done.returncode == 0, "UI smoke test failed:\n" + output[-8000:]
    assert "all checks passed" in done.stdout, output[-8000:]
    shots = sorted(p.name for p in Path(env["E2E_SCREENSHOT_DIR"]).glob("*.png"))
    for view in ("overview", "markets", "surges", "high", "strategy", "sim", "drawer"):
        for theme in ("light", "dark"):
            assert f"{view}-{theme}.png" in shots, shots
    assert "mobile-390.png" in shots, shots
