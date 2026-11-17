"""M5 稳健离群：MAD 稳健 z、IQR 回退、退化、阈值、优先级分区、首响裁决。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.modules.m5_outliers import flags, robust, zscore
from fixtures import dirty_cases, expected


def column_map(payload: dict) -> dict[str, dict]:
    return {item["name"]: item for item in payload["outliers"]["columns"]}


def test_mad_robust_z_matches_expected(dirty_result):
    """z = 0.6745 × (x − 中位数) / MAD，页面与导出都用这个值。"""
    columns = column_map(dirty_result)
    first = columns["first_response_min"]
    assert first["median"] == expected.OUTLIERS["first_response_min"]["median"]
    assert first["mad"] == expected.OUTLIERS["first_response_min"]["mad"]
    assert first["spread"] == expected.OUTLIERS["first_response_min"]["spread"]
    assert first["method"] == "mad"
    z_by_lineno = {(item["lineno"], item["value"]): item["z"]
                   for item in dirty_result["outliers"]["cells"]}
    assert z_by_lineno[(3, "1204.5")] == expected.OUTLIER_Z[(3, "1204.5")]
    assert z_by_lineno[(55, "1980.0")] == expected.OUTLIER_Z[(55, "1980.0")]


def test_iqr_fallback_when_mad_zero(settings):
    """MAD=0 但 IQR>0：回退 IQR/1.349，等价于 z = 1.349×(x−med)/IQR。"""
    payload = dirty_cases.pipeline(settings, dirty_cases.iqr_fallback_case())
    columns = column_map(payload)
    handle = columns["handle_hours"]
    assert handle["method"] == "iqr"
    spread = Decimal(handle["spread"])
    assert Decimal("0.09") < spread < Decimal("0.11")
    assert handle["outliers"] == 1
    outlier = payload["outliers"]["cells"][0]
    expected_z = Decimal("1.349") * (Decimal("2.60") - Decimal(handle["median"])) / Decimal(handle["iqr"])
    assert abs(Decimal(outlier["z"]) - expected_z) < Decimal("0.002")


def test_constant_column_is_degenerate(settings):
    payload = dirty_cases.pipeline(settings, dirty_cases.constant_case())
    columns = column_map(payload)
    handle = columns["handle_hours"]
    assert handle["method"] == "degenerate"
    assert handle["degenerate"] is True
    assert handle["outliers"] == 0
    assert "MAD" in handle["detail"]
    assert "handle_hours" in [item["column"] for item in payload["outliers"]["degenerate"]]


def test_low_resolution_column_is_degenerate(dirty_result):
    """1-5 分满意度：离散度低于「刻度 × 5」，明确判退化而不是硬判离群。"""
    satisfaction = column_map(dirty_result)["satisfaction"]
    assert satisfaction["degenerate"] is True
    assert satisfaction["floor"] == "5"
    assert "刻度下限" in satisfaction["detail"]
    assert satisfaction["tested"] == 0


def test_priority_missing_over_range_over_outlier(dirty_result):
    """TRAP-19：缺失、越界、离群互斥；每列三条判定加得回总行数。"""
    columns = column_map(dirty_result)
    rows = dirty_result["run"]["rows_accepted"]
    for name in ("first_response_min", "handle_hours", "satisfaction"):
        item = columns[name]
        assert item["values"] + dirty_cases_missing(dirty_result, name) + \
            dirty_cases_bad(dirty_result, name) == rows
        assert item["tested"] <= item["values"]


def dirty_cases_missing(result: dict, name: str) -> int:
    for item in result["profile"]["profile"]:
        if item["name"] == name:
            return item["missing"]
    raise AssertionError(name)


def dirty_cases_bad(result: dict, name: str) -> int:
    for item in result["profile"]["profile"]:
        if item["name"] == name:
            return item["format_bad"]
    raise AssertionError(name)


def test_outlier_cells_are_value_subset(settings):
    payload = dirty_cases.pipeline(
        settings, Path("sample_data/tickets_dirty.csv").read_text(encoding="utf-8"))
    rows = {row["lineno"]: row for row in payload["profile"]["rows"]}
    assert payload["outliers"]["cells"]
    for item in payload["outliers"]["cells"]:
        cell = rows[item["lineno"]]["cells"][item["column"]]
        assert cell["state"] == "value"
        assert cell["value"] == item["value"]


def test_z_limit_boundary():
    assert zscore.is_outlier(Decimal("3.5")) is False
    assert zscore.is_outlier(Decimal("-3.5")) is False
    assert zscore.is_outlier(Decimal("3.5001")) is True
    assert zscore.is_outlier(Decimal("-3.5001")) is True


def test_insufficient_sample_skips(settings):
    payload = dirty_cases.pipeline(settings, dirty_cases.short_case())
    for item in payload["outliers"]["columns"]:
        assert item["method"] == "insufficient"
        assert item["tested"] == 0
        assert item["outliers"] == 0


def test_median_and_mad_are_decimal():
    values = [Decimal(value) for value in ("1", "2", "3", "4", "100")]
    assert robust.median(values) == Decimal("3")
    assert robust.median([Decimal("1"), Decimal("2")]) == Decimal("1.5")
    assert robust.iqr(values) > Decimal("0")
    assert isinstance(robust.scale(values)["spread"], Decimal)


def test_outliers_api_route(client_with_run):
    body = client_with_run.get("/api/outliers").json()
    assert body["counts"]["outlier_cells"] == expected.DIRTY["outlier_cells"]
    assert body["degenerate"][0]["column"] == "satisfaction"
    assert len(body["cells"]) == expected.DIRTY["outlier_cells"]


def test_flags_module_marks_degenerate_columns(settings):
    payload = dirty_cases.pipeline(settings, dirty_cases.constant_case())
    marked = flags.mark(payload["profile"])
    assert marked["counts"]["degenerate_columns"] == 2
    assert marked["counts"]["outlier_cells"] == 0


def test_adjudication_splits_dirty_sample(dirty_result):
    """脏样例：6 个首响离群格 → 3 数据缺陷（行 3/35/55）+ 3 慢服务复核。"""
    adjudication = dirty_result["outliers"]["adjudication"]
    counts = adjudication["counts"]
    for key in ("flagged_cells", "quality_cells", "review_cells",
                "unverifiable_cells", "tickets", "quality_tickets",
                "review_tickets"):
        assert counts[key] == expected.ADJUDICATION[key], key
    assert [e["lineno"] for e in adjudication["defects"]] == \
        expected.ADJUDICATION["defect_linenos"]
    assert [e["lineno"] for e in adjudication["reviews"]] == \
        expected.ADJUDICATION["review_linenos"]
    assert {e["code"] for e in adjudication["defects"]} == {"K_RESPONSE_GT_HANDLE"}
    assert {e["code"] for e in adjudication["reviews"]} == {"K_SLOW_SERVICE"}


def test_adjudication_rule_boundary(settings):
    """裁决只看物理不变式：>上限 判缺陷；≤上限、无法核对 都进复核。"""
    payload = dirty_cases.pipeline(settings, dirty_cases.adjudication_case())
    adjudication = payload["outliers"]["adjudication"]
    counts = adjudication["counts"]
    assert counts["quality_cells"] == 1
    assert counts["review_cells"] == 2
    assert counts["unverifiable_cells"] == 1
    defect = adjudication["defects"][0]
    assert defect["ticket_id"] == "T-2026-950001"
    assert defect["limit_min"] == "150.00"
    reviews = {e["ticket_id"]: e for e in adjudication["reviews"]}
    assert reviews["T-2026-950002"]["limit_min"] == "1200.00"
    assert reviews["T-2026-950003"]["handle_hours"] == ""
    assert "无法核对" in reviews["T-2026-950003"]["note"]


def test_adjudication_cells_partition_no_loss(dirty_result):
    """首响离群格 = 质量异常 + 慢服务复核；一格只进一个范围，不丢记录。"""
    outliers = dirty_result["outliers"]
    counts = outliers["adjudication"]["counts"]
    assert counts["flagged_cells"] == counts["quality_cells"] + counts["review_cells"]
    flagged = [cell for cell in outliers["cells"]
               if cell["column"] == "first_response_min"]
    assert len(flagged) == counts["flagged_cells"]
    verdicts = [cell.get("verdict") for cell in flagged]
    assert verdicts.count("quality") == counts["quality_cells"]
    assert verdicts.count("review") == counts["review_cells"]
    # 非首响列的离群格（如 handle_hours）不参与裁决、不得带结论
    others = [cell for cell in outliers["cells"]
              if cell["column"] != "first_response_min"]
    assert others and all("verdict" not in cell for cell in others)


def test_adjudication_ticket_scopes_exclusive(dirty_result):
    """同一工单只进一个范围；同工单多行缺陷合并，工单账两边加得回。"""
    adjudication = dirty_result["outliers"]["adjudication"]
    counts = adjudication["counts"]
    tickets = adjudication["tickets"]
    assert counts["tickets"] == counts["quality_tickets"] + counts["review_tickets"]
    ids = [item["ticket_id"] for item in tickets]
    assert len(ids) == len(set(ids))
    quality = {item["ticket_id"] for item in tickets if item["scope"] == "quality"}
    review = {item["ticket_id"] for item in tickets if item["scope"] == "review"}
    assert not (quality & review)
    assert quality == set(expected.ADJUDICATION["defect_tickets"])
    merged = {item["ticket_id"]: item["linenos"] for item in tickets}
    assert merged["T-2026-000102"] == [3, 35]


def test_adjudication_entries_trace_to_source(settings):
    """每条结论都带行号、工单号、原值，逐条能回到原始工单。"""
    payload = dirty_cases.pipeline(
        settings, Path("sample_data/tickets_dirty.csv").read_text(encoding="utf-8"))
    adjudication = payload["outliers"]["adjudication"]
    rows = {row["lineno"]: row for row in payload["profile"]["rows"]}
    entries = adjudication["defects"] + adjudication["reviews"]
    assert entries
    for entry in entries:
        row = rows[entry["lineno"]]
        assert row["cells"]["ticket_id"]["raw"] == entry["ticket_id"]
        cell = row["cells"]["first_response_min"]
        assert cell["raw"] == entry["raw"]
        assert cell["value"] == entry["value"]


def test_slow_service_cells_not_deducted(dirty_result):
    """质量扣分只反映数据缺陷：计扣离群格 = 离群格 − 慢服务复核格。"""
    counts = dirty_result["outliers"]["counts"]
    assert counts["quality_outlier_cells"] == \
        counts["outlier_cells"] - counts["slow_service_cells"]
    dims = {item["key"]: item for item in dirty_result["score"]["dims"]}
    assert dims["outlier"]["numerator"] == counts["quality_outlier_cells"]
