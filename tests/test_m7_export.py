"""M7 报告导出：CSV 编码、内容稳定、证据行、清单、空态。"""
from __future__ import annotations

import json

from app import config
from fixtures import expected


def get_csv(client, name: str) -> str:
    response = client.get(f"/api/export/{name}")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    return response.content.decode("utf-8")


def test_report_csv_has_bom_and_core_rows(client_with_run):
    text = get_csv(client_with_run, "report.csv")
    assert text.startswith("\ufeff")
    assert "\r\n" in text
    assert f"总分,{expected.DIRTY['score']},分" in text
    assert "扣分合计,28.64,分" in text
    assert f"等级,{expected.DIRTY['grade']}" in text


def test_report_csv_carries_adjudication_counters(client_with_run):
    """报告 CSV 与页面同口径：计扣离群格、两个范围的格数与工单数。"""
    text = get_csv(client_with_run, "report.csv")
    assert "计扣离群格,4,格" in text
    assert "首响离群格,6,格" in text
    assert "质量异常格,3,格" in text
    assert "慢服务复核格,3,格" in text
    assert "质量异常工单,2,工单" in text
    assert "慢服务复核工单,3,工单" in text


def test_csv_output_is_stable_across_requests(client_with_run):
    first = get_csv(client_with_run, "report.csv")
    second = get_csv(client_with_run, "report.csv")
    assert first == second
    profile_first = get_csv(client_with_run, "profile.csv")
    assert profile_first == get_csv(client_with_run, "profile.csv")


def test_quarantine_csv_carries_lineno_and_code(client_with_run):
    text = get_csv(client_with_run, "quarantine.csv")
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines[0].lstrip("\ufeff").startswith("行号,段,原因码")
    assert len(lines) == expected.DIRTY["quarantine"] + 1
    assert "44,2,Q_COLUMN_COUNT" in text
    assert "57,2,Q_TRUNCATED_ROW" in text


def test_profile_csv_lists_contract_columns(client_with_run):
    text = get_csv(client_with_run, "profile.csv")
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == len(config.COLUMN_NAMES) + 1
    for name in config.COLUMN_NAMES:
        assert name in text


def test_duplicates_csv_lists_conflict_fields(client_with_run):
    text = get_csv(client_with_run, "duplicates.csv")
    assert "业务键冲突,T-2026-000204,2" in text
    assert "satisfaction agent" in text
    assert "全行哈希" in text


def test_outliers_csv_matches_cells(client_with_run):
    text = get_csv(client_with_run, "outliers.csv")
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == expected.DIRTY["outlier_cells"] + 1
    assert "1204.5" in text
    assert "K_OUTLIER" in text


def test_outliers_csv_carries_verdict_columns(client_with_run):
    """离群导出与页面同口径：裁决范围、裁决原因码、是否计扣。"""
    text = get_csv(client_with_run, "outliers.csv")
    header = text.splitlines()[0].lstrip("\ufeff")
    assert header.endswith("裁决范围,裁决原因码,计扣")
    assert "质量异常,K_RESPONSE_GT_HANDLE,是" in text
    assert "慢服务复核,K_SLOW_SERVICE,否" in text
    # 计扣=是 的行数 = 计扣离群格（3 首响缺陷 + 1 处理时长离群）
    charged = [line for line in text.splitlines()[1:] if line.endswith(",是")]
    assert len(charged) == expected.DIRTY["quality_outlier_cells"]


def test_adjudication_csv_lists_both_scopes(client_with_run):
    """裁决导出：两个范围同出一份，逐格带工单号与行号，行数 = 首响离群格。"""
    text = get_csv(client_with_run, "adjudication.csv")
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines[0].lstrip("\ufeff").startswith("范围,工单号,行号,")
    assert len(lines) == expected.ADJUDICATION["flagged_cells"] + 1
    quality = [line for line in lines[1:] if line.startswith("质量异常,")]
    review = [line for line in lines[1:] if line.startswith("慢服务复核,")]
    assert len(quality) == expected.ADJUDICATION["quality_cells"]
    assert len(review) == expected.ADJUDICATION["review_cells"]
    assert "质量异常,T-2026-000102,3," in text
    assert "质量异常,T-2026-000155,55," in text
    assert "慢服务复核,T-2026-000110,11," in text
    assert "K_RESPONSE_GT_HANDLE" in text
    assert "K_SLOW_SERVICE" in text


def test_missing_csv_has_runs_columns(client_with_run):
    text = get_csv(client_with_run, "missing.csv")
    assert "列名,列标题,缺失,有值,缺失率,连续段数,最长段" in text
    assert "agent,受理人,5,46,0.0980,1,4" in text


def test_manifest_json_matches_run(client_with_run):
    latest = client_with_run.get("/api/score/latest").json()
    response = client_with_run.get("/api/export/manifest.json")
    assert response.status_code == 200
    payload = json.loads(response.content)
    assert payload["run_id"] == latest["run"]["run_id"]
    assert payload["input_digest"] == latest["run"]["digest"]
    assert payload["score"] == expected.DIRTY["score"]
    assert len(payload["accounts"]) == 7
    assert payload["all_balanced"] is True
    assert isinstance(payload["weights"], list) and len(payload["weights"]) == 4
    assert payload["counters"]["quality_outlier_cells"] == \
        expected.DIRTY["quality_outlier_cells"]
    assert payload["counters"]["slow_service_cells"] == \
        expected.DIRTY["slow_service_cells"]
    assert payload["adjudication_rule"]["defect_code"] == "K_RESPONSE_GT_HANDLE"


def test_export_without_run_returns_404(client):
    response = client.get("/api/export/report.csv")
    assert response.status_code == 404
    assert response.json()["error"] == "E_RUN_NOT_FOUND"


def test_report_page_renders_score_and_bars(client_with_run):
    response = client_with_run.get("/")
    assert response.status_code == 200
    html = response.text
    assert expected.DIRTY["score"] in html
    assert html.count('class="bar"') == 4
    assert html.count('class="chart__col"') == 7
    assert 'src="http' not in html
    assert "没有运行记录" not in html


def test_empty_report_page_shows_single_empty_state(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.text.count("还没有运行记录。") == 1
