#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_facts.py —— 把脚本输出的 JSON 变成 report.py 能用的 facts.json

为什么要多这一步：
    报告里的关键数字**必须由程序产出**，不能在写报告时手工转抄 ——
    手抄是"数字在某处悄悄变了"的主要来源。本脚本只做搬运和标注，
    不改动任何数值，并把 `--source` 一并写进去，让每条数字都可追溯。

用法：
    # 1) 从 cohort.py 的输出里取一条比例
    python3 scripts/make_facts.py --rate-json 成熟批次.json \\
        --label "24小时窗口内核销订单比例" \\
        --source "订单表.csv + 核销表.csv（先按订单聚合再关联）" \\
        --out facts.json

    # 2) 或者直接给一个值（仍需标注来源）
    python3 scripts/make_facts.py --value 1.0198 --label "总体ROI" \\
        --unit "" --source "计划对比.csv" --formula "Σ成交/Σ消耗" --out facts.json

    # 3) 多条：把若干 facts.json 合并
    python3 scripts/make_facts.py --merge a.json b.json --out facts.json
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _load(path: str) -> Dict[str, Any]:
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)


def from_rate_json(path: str, label: Optional[str], source: Optional[str],
                   as_percent: Optional[bool] = None) -> Dict[str, Any]:
    """从 cohort.py --json 的输出里取 rate（或 curve 的第一条）构造一条事实。"""
    d = _load(path)
    r = d.get("rate")
    if r is None and isinstance(d.get("curve"), list) and d["curve"]:
        r = d["curve"][0]
    if r is None and "value" in d:
        r = d
    if r is None:
        raise ValueError("在 %s 中找不到 rate / curve / value 字段" % path)
    item: Dict[str, Any] = dict(r)
    item["label"] = label or "H窗口比例"
    if source:
        item["source"] = source
    # 比例类结果必须按百分比显示，否则 0.1578 会被误读成"0.16"
    if as_percent is None:
        meta = item.get("meta") or {}
        looks_like_rate = ("n_eligible" in meta) or ("n_hit" in meta)
        unit_is_ratio = item.get("unit") in ("", None, "%")
        as_percent = bool(looks_like_rate and unit_is_ratio)
    item["as_percent"] = bool(as_percent)
    return item


def from_value(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "label": args.label or "未命名指标",
        "value": args.value,
        "unit": args.unit or "",
        "numerator": args.numerator,
        "denominator": args.denominator,
        "formula": args.formula or "",
        "scope": args.scope or "",
        "source": args.source or "",
        "note": args.note or "",
        "as_percent": bool(args.as_percent),
    }


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="把脚本 JSON 转成 report.py 用的 facts.json")
    p.add_argument("--rate-json", dest="rate_json", default=None,
                   help="cohort.py --json 的输出文件")
    p.add_argument("--value", default=None, help="直接给一个数值")
    p.add_argument("--label", default=None)
    p.add_argument("--unit", default=None)
    p.add_argument("--numerator", default=None)
    p.add_argument("--denominator", default=None)
    p.add_argument("--formula", default=None)
    p.add_argument("--scope", default=None)
    p.add_argument("--source", default=None, help="来源（文件/工作表/时间筛选）")
    p.add_argument("--note", default=None, help="这条数字的限制")
    p.add_argument("--merge", nargs="*", default=None, help="合并多个 facts.json")
    p.add_argument("--as-percent", dest="as_percent", action="store_true",
                   help="把这条数字按百分比显示（比例类指标请加）；不给则按原值显示")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    facts: List[Dict[str, Any]] = []
    if args.rate_json:
        facts.append(from_rate_json(args.rate_json, args.label, args.source,
                                    as_percent=(True if args.as_percent else None)))
    if args.value is not None:
        facts.append(from_value(args))
    if args.merge:
        for path in args.merge:
            d = _load(path)
            if isinstance(d, dict) and isinstance(d.get("facts"), list):
                facts.extend(d["facts"])
            elif isinstance(d, list):
                facts.extend(d)

    if not facts:
        sys.stderr.write("没有可写入的事实。请用 --rate-json / --value / --merge 之一。\n")
        return 2

    for f in facts:
        if not f.get("source"):
            sys.stderr.write("⚠️ 提示：`%s` 缺少 --source，报告里将无法追溯来源。\n"
                             % f.get("label", "未命名"))

    with io.open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"facts": facts}, fh, ensure_ascii=False, indent=2)
    print("已写入 %s（%d 条）" % (args.out, len(facts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
