"""End-to-end browser test of the dashboard UI (Playwright via node).

Runs ``tests/e2e/ui_smoke.cjs`` against ``tests/e2e/serve_demo.py`` (the simulated market: no
API key, no network). It is skipped when node or the Playwright package is not installed.
Screenshots go to a temporary folder here; run ``node tests/e2e/ui_smoke.cjs`` directly to
refresh ``docs/screenshots/``. Deselect with ``pytest -m "not e2e"``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "tests" / "e2e" / "ui_smoke.cjs"
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


@pytest.mark.e2e
def test_dashboard_ui_smoke(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    if not _have_playwright(node):
        pytest.skip(f"the playwright package is not available at {_playwright_module()}")
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
    for view in ("overview", "markets", "surges", "high", "strategy", "drawer"):
        for theme in ("light", "dark"):
            assert f"{view}-{theme}.png" in shots, shots
    assert "mobile-390.png" in shots, shots
