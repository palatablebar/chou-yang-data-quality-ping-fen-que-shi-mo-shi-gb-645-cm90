"""首响离群裁决：把稳健离群旗标的首响长值分成「数据缺陷」与「慢服务复核」。

裁决规则（常量唯一来源 app/config.py）：
- 同行 handle_hours 为合格有值，且 first_response_min > handle_hours × 60：
  首响超过总处理时长，物理上不可能，判数据缺陷（K_RESPONSE_GT_HANDLE），
  计入离群维扣分；
- 其余（物理自洽的长首响，或处理时长缺失/格式不合格无法证伪）：
  进慢服务复核（K_SLOW_SERVICE），不参与质量扣分，交服务侧复核。

两条分区恒等式（页面、API、导出、清单同口径）：
- 首响离群格 = 质量异常格 + 慢服务复核格（分类不丢记录）；
- 裁决工单 = 质量异常工单 + 慢服务复核工单（同一工单只进一个范围，
  同一工单多行结论冲突时数据缺陷优先）。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config

QUALITY = "quality"
REVIEW = "review"
SCOPE_LABELS = {QUALITY: "质量异常", REVIEW: "慢服务复核"}


def split(rows: list[dict], cells: list[dict]) -> tuple[dict, dict[int, str]]:
    """对首响离群格逐格裁决。

    返回 (裁决 payload, {行号: 范围})；payload 里 defects / reviews 都带
    行号、工单号、原值，任何一条结论都能回到原始工单。
    """
    by_lineno = {row["lineno"]: row for row in rows}
    defects: list[dict] = []
    reviews: list[dict] = []
    verdicts: dict[int, str] = {}
    unverifiable = 0
    flagged = [cell for cell in cells
               if cell["column"] == config.RESPONSE_COLUMN]
    for cell in sorted(flagged, key=lambda item: item["lineno"]):
        row = by_lineno[cell["lineno"]]
        ticket = row["cells"]["ticket_id"]
        handle = row["cells"][config.HANDLE_COLUMN]
        entry = {
            "lineno": cell["lineno"],
            "ticket_id": ticket["raw"] if ticket["state"] == "value" else "",
            "value": cell["value"],
            "raw": cell["raw"],
            "z": cell["z"],
            "handle_hours": handle["value"] if handle["state"] == "value" else "",
            "limit_min": "",
            "code": "",
            "note": "",
        }
        limit = _handle_limit(handle)
        if limit is not None:
            entry["limit_min"] = str(limit)
            if Decimal(cell["value"]) > limit:
                entry["code"] = "K_RESPONSE_GT_HANDLE"
                entry["note"] = (f"首响 {cell['value']} 分钟 > 处理时长上限 "
                                 f"{limit} 分钟，明显录错")
                defects.append(entry)
                verdicts[cell["lineno"]] = QUALITY
                continue
            entry["note"] = f"首响未超处理时长上限 {limit} 分钟，物理自洽"
        else:
            unverifiable += 1
            entry["note"] = _unverifiable_note(handle)
        entry["code"] = "K_SLOW_SERVICE"
        reviews.append(entry)
        verdicts[cell["lineno"]] = REVIEW
    tickets = _ticket_scopes(defects, reviews)
    counts = {
        "flagged_cells": len(defects) + len(reviews),
        "quality_cells": len(defects),
        "review_cells": len(reviews),
        "unverifiable_cells": unverifiable,
        "tickets": len(tickets),
        "quality_tickets": sum(1 for item in tickets if item["scope"] == QUALITY),
        "review_tickets": sum(1 for item in tickets if item["scope"] == REVIEW),
    }
    payload = {
        "rule": {
            "column": config.RESPONSE_COLUMN,
            "compare_column": config.HANDLE_COLUMN,
            "factor": str(config.HANDLE_TO_MINUTES),
            "defect_code": "K_RESPONSE_GT_HANDLE",
            "review_code": "K_SLOW_SERVICE",
            "text": ("首响(分钟) > 处理时长(小时) × 60 → 数据缺陷，计入质量扣分；"
                     "否则进慢服务复核，不计扣"),
        },
        "counts": counts,
        "defects": defects,
        "reviews": reviews,
        "tickets": tickets,
    }
    return payload, verdicts


def _handle_limit(cell: dict) -> Decimal | None:
    """处理时长换算成的首响上限（分钟）；缺失或格式不合格时无法核对。"""
    if cell["state"] != "value" or cell.get("value") in (None, ""):
        return None
    return Decimal(cell["value"]) * config.HANDLE_TO_MINUTES


def _unverifiable_note(cell: dict) -> str:
    if cell["state"] == "missing":
        return "处理时长缺失，无法核对，按慢服务复核"
    return "处理时长格式不合格，无法核对，按慢服务复核"


def _ticket_scopes(defects: list[dict], reviews: list[dict]) -> list[dict]:
    """工单级归并：同一工单只进一个范围，结论冲突时数据缺陷优先。"""
    tickets: dict[str, dict] = {}
    for scope, entries in ((QUALITY, defects), (REVIEW, reviews)):
        for entry in entries:
            key = entry["ticket_id"] or f"#{entry['lineno']}"
            slot = tickets.setdefault(key, {"ticket_id": entry["ticket_id"],
                                            "scope": REVIEW, "linenos": []})
            if scope == QUALITY:
                slot["scope"] = QUALITY
            slot["linenos"].append(entry["lineno"])
    ordered = sorted(tickets.values(), key=lambda item: min(item["linenos"]))
    for item in ordered:
        item["linenos"].sort()
        item["scope_label"] = SCOPE_LABELS[item["scope"]]
    return ordered
