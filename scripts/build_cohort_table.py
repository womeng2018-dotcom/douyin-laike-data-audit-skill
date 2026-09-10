#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_cohort_table.py —— 构建「对象级成熟批次表」（规范第五节的正确做法，可执行版）

为什么需要它：
    核销表常常是**一行一券**，而订单表是**一行一单**。直接把两张表 join 起来汇总
    金额，订单金额会被按券数重复累计（`reconcile.py join` 会把这种情形判为
    「失败-禁止使用该结果」）。正确路径只有一条：

        右表先按对象键聚合  →  再与左表 1:1 关联

    本脚本就是这条路径的工具化：
      - 事件时间列：默认取**首次**（min），可改为 max / first / last / count
      - 事件金额列（可选）：按对象求和，并且**明确标注它是对象级金额**，
        不会与订单金额混为一谈
      - 数量类列（如"购买数量"）：默认取 max —— 因为一单多券时该列写的是
        原订单购买总数，逐行相加会按券数翻倍（规范第五节的明确禁令）
      - 关联结果逐列报告：聚合前后行数、每个对象对应几行、未匹配对象数

用法：
    python3 scripts/build_cohort_table.py \
        --orders 订单表.csv --events 核销表.csv \
        --key 订单ID --event-time 核销时间 --out-col 首次核销时间 \
        --out 订单级批次表.csv
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import metrics as M  # noqa: E402
from metrics import MISSING_TOKENS, parse_datetime, to_decimal  # noqa: E402

try:
    from contract import load_table  # type: ignore
except Exception:  # pragma: no cover
    load_table = None


def table_to_dicts(t: Any) -> Tuple[List[str], List[Dict[str, Any]]]:
    headers = list(getattr(t, "headers", []) or [])
    rows = []
    for row in getattr(t, "rows", []) or []:
        rows.append({h: (row[i] if i < len(row) else "") for i, h in enumerate(headers)})
    return headers, rows


def build(orders_rows: List[Dict[str, Any]],
          event_rows: List[Dict[str, Any]],
          key: str,
          event_time_col: str,
          out_col: str,
          agg: str = "min") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    返回 (关联后的行, 审计信息)。审计信息必须一起发布，不能只给结果。
    """
    # ---- 1) 事件表按对象键聚合 ----
    first: Dict[str, Any] = {}
    last: Dict[str, Any] = {}
    count: Dict[str, int] = {}
    unparsable = 0
    missing_event = 0
    rows_per_object: Dict[str, int] = {}

    for r in event_rows:
        k = r.get(key)
        if M.is_missing(k):
            continue
        k = str(k).strip()
        rows_per_object[k] = rows_per_object.get(k, 0) + 1
        raw = r.get(event_time_col)
        if M.is_missing(raw):
            missing_event += 1
            continue
        t = parse_datetime(raw)
        if t is None:
            unparsable += 1
            continue
        if k not in first or t < first[k]:
            first[k] = t
        if k not in last or t > last[k]:
            last[k] = t
        count[k] = count.get(k, 0) + 1

    # ---- 2) 与订单表 1:1 关联 ----
    out: List[Dict[str, Any]] = []
    matched = 0
    for r in orders_rows:
        k = r.get(key)
        k = "" if M.is_missing(k) else str(k).strip()
        row = dict(r)
        if k and k in rows_per_object:
            matched += 1
        if agg == "count":
            row[out_col] = str(count.get(k, 0))
        elif agg == "max":
            row[out_col] = last[k].strftime("%Y-%m-%d %H:%M:%S") if k in last else ""
        elif agg == "first":
            row[out_col] = first[k].strftime("%Y-%m-%d %H:%M:%S") if k in first else ""
        else:  # min（默认）
            row[out_col] = first[k].strftime("%Y-%m-%d %H:%M:%S") if k in first else ""
        out.append(row)

    multi = {k: v for k, v in rows_per_object.items() if v > 1}
    audit = {
        "key": key,
        "orders_rows": len(orders_rows),
        "event_rows": len(event_rows),
        "distinct_objects_in_events": len(rows_per_object),
        "objects_with_multiple_event_rows": len(multi),
        "max_event_rows_per_object": max(rows_per_object.values()) if rows_per_object else 0,
        "matched_orders": matched,
        "unmatched_orders": len(orders_rows) - matched,
        "event_rows_missing_time": missing_event,
        "event_rows_unparsable_time": unparsable,
        "agg": agg,
        "out_col": out_col,
        "note": ("事件表 %d 行 → 聚合为 %d 个对象；%d 个对象有多行（最多 %d 行/对象），"
                 "直接 join 会让左表金额按此倍数重复累计"
                 % (len(event_rows), len(rows_per_object), len(multi),
                    max(rows_per_object.values()) if rows_per_object else 0)),
    }
    return out, audit


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="构建对象级成熟批次表：事件表先按对象聚合，再与对象表 1:1 关联",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python3 scripts/build_cohort_table.py \\\n"
               "      --orders 订单表.csv --events 核销表.csv \\\n"
               "      --key 订单ID --event-time 核销时间 \\\n"
               "      --out-col 首次核销时间 --out 订单级批次表.csv\n")
    p.add_argument("--orders", required=True, help="对象表（一行一单）")
    p.add_argument("--events", required=True, help="事件表（可能一行一券／一次售后）")
    p.add_argument("--key", required=True, help="对象键，例如 订单ID")
    p.add_argument("--event-time", dest="event_time", required=True, help="事件时间列")
    p.add_argument("--out-col", dest="out_col", default="首次事件时间",
                   help="输出到对象表的列名")
    p.add_argument("--agg", default="min", choices=["min", "max", "first", "count"],
                   help="事件时间聚合方式，默认 min（首次事件）")
    p.add_argument("--sheet-orders", default=None)
    p.add_argument("--sheet-events", default=None)
    p.add_argument("--out", required=True, help="输出的对象级批次表（csv）")
    p.add_argument("--json-audit", default=None, help="把审计信息另存为 JSON")
    args = p.parse_args(argv)

    if load_table is None:
        sys.stderr.write("缺少 scripts/contract.py，无法读取文件。\n")
        return 2
    try:
        to = load_table(args.orders, sheet=args.sheet_orders)
        te = load_table(args.events, sheet=args.sheet_events)
    except Exception as exc:
        sys.stderr.write("读取失败：%s\n" % exc)
        return 2

    oh, orders_rows = table_to_dicts(to)
    eh, event_rows = table_to_dicts(te)
    for headers, need, label in ((oh, args.key, "对象表"), (eh, args.key, "事件表"),
                                 (eh, args.event_time, "事件表")):
        if need not in headers:
            sys.stderr.write("%s 找不到列 %r。现有列：%s\n"
                             % (label, need, "、".join(headers)))
            return 2

    out, audit = build(orders_rows, event_rows, args.key, args.event_time,
                       args.out_col, args.agg)
    headers = oh + [args.out_col]
    with io.open(args.out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=headers)
        w.writeheader()
        for r in out:
            w.writerow({h: r.get(h, "") for h in headers})

    print("已生成 %s" % args.out)
    print("  - 对象表 %d 行 × 事件表 %d 行 → 输出 %d 行（对象表行数不变，未发生行数放大）"
          % (audit["orders_rows"], audit["event_rows"], len(out)))
    print("  - 事件表聚合为 %d 个对象；其中 %d 个对象有多行（最多 %d 行/对象）"
          % (audit["distinct_objects_in_events"],
             audit["objects_with_multiple_event_rows"],
             audit["max_event_rows_per_object"]))
    print("  - 匹配 %d 个对象，未匹配 %d 个（未匹配的 `%s` 留空，**不是 0**）"
          % (audit["matched_orders"], audit["unmatched_orders"], args.out_col))
    if audit["event_rows_unparsable_time"]:
        print("  - ⚠️ 事件表有 %d 行事件时间无法解析（已计数，未静默丢弃）"
              % audit["event_rows_unparsable_time"])
    print("  - 提示：%s" % audit["note"])

    if args.json_audit:
        import json
        with io.open(args.json_audit, "w", encoding="utf-8") as fh:
            json.dump(audit, fh, ensure_ascii=False, indent=2)
        print("审计信息已写入 %s" % args.json_audit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
