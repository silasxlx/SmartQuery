from __future__ import annotations

import pandas as pd

from excel_agent.excel_loader import get_loader, reset_loader
from excel_agent.tools import aggregate_data, filter_data, generate_chart, group_and_aggregate


def test_legacy_filter_aggregate_group_and_chart_regression(tmp_path):
    reset_loader()
    path = tmp_path / "sample.xlsx"
    pd.DataFrame(
        {
            "branch": ["A", "A", "B", "B"],
            "amount": [100.0, 120.0, 80.0, 150.0],
            "orders": [10, 12, 8, 15],
        }
    ).to_excel(path, index=False)
    get_loader().add_table(str(path))

    filtered = filter_data.invoke({"column": "branch", "operator": "==", "value": "A"})
    assert filtered["total_rows"] == 2

    total = aggregate_data.invoke({"column": "amount", "agg_func": "sum"})
    assert total["result"] == 450.0

    grouped = group_and_aggregate.invoke(
        {"group_by": "branch", "agg_column": "amount", "agg_func": "sum"}
    )
    assert grouped["data"][0]["branch"] == "B"
    assert grouped["data"][0]["amount_sum"] == 230.0

    chart = generate_chart.invoke(
        {"chart_type": "bar", "x_column": "branch", "y_column": "amount", "agg_func": "sum"}
    )
    assert "chart" in chart
    assert chart["chart_type"] == "bar"
    reset_loader()
