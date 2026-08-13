from pathlib import Path

import pandas as pd
import yaml


def test_golden_manifest_has_thirty_cases_and_evidence():
    path = Path(__file__).parents[1] / "golden" / "golden_questions.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = manifest["cases"]
    assert len(cases) == 30
    assert len({case["id"] for case in cases}) == 30
    assert all(case["evidence"] for case in cases)
    xlsx = path.parents[1] / "fixtures" / "sample.xlsx"
    assert xlsx.exists()
    assert pd.ExcelFile(xlsx).sheet_names
