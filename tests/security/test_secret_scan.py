import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
SCAN_MODULE_SPEC = importlib.util.spec_from_file_location(
    "scan_secrets", ROOT / "scripts" / "scan_secrets.py"
)
assert SCAN_MODULE_SPEC and SCAN_MODULE_SPEC.loader
SCAN_MODULE = importlib.util.module_from_spec(SCAN_MODULE_SPEC)
SCAN_MODULE_SPEC.loader.exec_module(SCAN_MODULE)


def test_secret_scan_passes_repository():
    root = Path(__file__).parents[2]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "scan_secrets.py")],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout


def test_secret_scan_detects_tokens_with_dots_and_hyphens():
    sample = "sk-" + "ws-H.EHMEHHM.6Z3O." + "MEYCIQCTcTGDcTGDcTGDcTGD"
    assert any(pattern.search(sample) for pattern in SCAN_MODULE.PATTERNS)
