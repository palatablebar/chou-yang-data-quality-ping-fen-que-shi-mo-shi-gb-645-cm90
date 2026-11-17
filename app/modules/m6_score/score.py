"""评分：四个维度各自封顶，总分 = 100 − 各维扣分。

每维扣分 = 封顶分 × min(1, 实际比率 / 容忍度)，量化到 0.01（ROUND_HALF_UP）。
封顶是硬上限：比率再高也不会把该维扣成负分，所以总分恒在 [0, 100]，
且「总分 + 扣分之和 = 100」逐位成立。
离群维分子只计数据缺陷格（quality_outlier_cells）：慢服务复核是服务表现，
不是数据缺陷，不参与质量扣分。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from ... import config
from . import grade

ZERO = Decimal("0")


def rate(numerator: int, denominator: int) -> Decimal:
    if denominator is None or denominator <= 0:
        return Decimal("0.0000")
    return (Decimal(numerator) / Decimal(denominator)).quantize(
        config.RATE_QUANT, rounding=ROUND_HALF_UP)


def quality_outliers(counts: dict) -> int:
    """计扣离群格：只含数据缺陷（慢服务复核格不扣）。

    缺省回退旧口径（全部离群格计扣），保证只传 outlier_cells 的调用方
    结果不变。
    """
    if "quality_outlier_cells" in counts:
        return counts["quality_outlier_cells"]
    return counts.get("outlier_cells", 0)


def ratios(counts: dict) -> dict:
    cells_total = counts.get("cells_total", 0)
    cells_missing = counts.get("cells_missing", 0)
    non_missing = cells_total - cells_missing
    return {
        "missing": rate(cells_missing, cells_total),
        "duplicate": rate(counts.get("rows_dup", 0), counts.get("rows_accepted", 0)),
        "outlier": rate(quality_outliers(counts), counts.get("tested_cells", 0)),
        "format": rate(counts.get("format_bad", 0), non_missing),
    }


def penalties(counts: dict) -> list[dict]:
    actual = ratios(counts)
    numerators = {
        "missing": counts.get("cells_missing", 0),
        "duplicate": counts.get("rows_dup", 0),
        "outlier": quality_outliers(counts),
        "format": counts.get("format_bad", 0),
    }
    denominators = {
        "missing": counts.get("cells_total", 0),
        "duplicate": counts.get("rows_accepted", 0),
        "outlier": counts.get("tested_cells", 0),
        "format": counts.get("cells_total", 0) - counts.get("cells_missing", 0),
    }
    rows = []
    for item in config.WEIGHTS:
        key = item["key"]
        cap = Decimal(item["cap"])
        tolerance = Decimal(item["tolerance"])
        value = actual[key]
        ratio = (value / tolerance) if tolerance > 0 else ZERO
        capped = ratio > 1
        ratio = min(ratio, Decimal("1"))
        penalty = (cap * ratio).quantize(config.SCORE_QUANT, rounding=ROUND_HALF_UP)
        severity = grade.severity(penalty, cap)
        rows.append({
            "key": key,
            "label": item["label"],
            "cap": str(cap),
            "tolerance": str(tolerance),
            "numerator": numerators[key],
            "denominator": denominators[key],
            "rate": str(value),
            "ratio": str(ratio.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)),
            "penalty": str(penalty),
            "capped": capped,
            "severity": severity["key"],
            "severity_label": severity["label"],
        })
    return rows


def evaluate(counts: dict) -> dict:
    dims = penalties(counts)
    penalty_sum = sum((Decimal(item["penalty"]) for item in dims), ZERO)
    penalty_sum = penalty_sum.quantize(config.SCORE_QUANT, rounding=ROUND_HALF_UP)
    total = (config.HUNDRED - penalty_sum).quantize(config.SCORE_QUANT,
                                                   rounding=ROUND_HALF_UP)
    total = min(max(total, ZERO), config.HUNDRED)
    graded = grade.of(total)
    return {
        "score": str(total),
        "grade": graded["grade"],
        "grade_label": graded["label"],
        "grade_floor": graded["floor"],
        "penalty_sum": str(penalty_sum),
        "dims": dims,
        "bands": grade.bands(dims),
        "closed": (total + penalty_sum == config.HUNDRED),
        "weights": [{"key": item["key"], "label": item["label"], "cap": item["cap"],
                     "tolerance": item["tolerance"]} for item in config.WEIGHTS],
        "formula": "100 − 缺失30 − 重复25 − 离群20 − 格式25，每维按容忍度封顶；"
                   "离群维只计数据缺陷格，慢服务复核格不扣",
    }
