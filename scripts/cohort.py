#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cohort.py —— 同批次、同观察年龄的核销／退款分析（规范第九节）

为什么单独做一个命令：
    "核销率"是本地生活经营里最容易被算错的指标。最常见的错误是
    **把今天核销额除以今天成交额**，或者**用只支付了 3 小时的订单**
    去和支付了 5 天的订单比。这个脚本把"成熟条件"变成一道程序闸门：

        t0 = 每笔订单的支付时间
        T  = 可靠的数据观察截止时间（必须由用户明确给出）
        H  = 观察时长（24 小时 / 72 小时 / 7 天 …）
        可进入 H 窗口分析 ⇔ t0 + H <= T

用法：
    python3 scripts/cohort.py --file 订单表.csv \
        --t0 支付时间 --event 核销时间 \
        --cutoff "2026-09-10 12:00:00" --hours 24 --dedupe 订单ID

    # 同一成熟批次的多个观察节点（各节点可相减得到增量）
    python3 scripts/cohort.py --file 订单表.csv --t0 支付时间 --event 核销时间 \
        --cutoff "2026-09-10 12:00:00" --curve 1,6,24,72,168 --dedupe 订单ID

    # 退款同理，把 --event 换成退款时间即可
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import metrics as M  # noqa: E402

try:
    from contract import load_table  # type: ignore
except Exception:  # pragma: no cover
    load_table = None


def table_to_dicts(t: Any) -> List[Dict[str, Any]]:
    """把 contract.Table 转成 [{列名: 值}, ...]。所有值保持字符串。"""
    headers = list(getattr(t, "headers", []) or [])
    out: List[Dict[str, Any]] = []
    for row in getattr(t, "rows", []) or []:
        d: Dict[str, Any] = {}
        for i, h in enumerate(headers):
            d[h] = row[i] if i < len(row) else ""
        out.append(d)
    return out


def _fmt(r: M.Result, as_percent: bool = True, digits: int = 2) -> str:
    return r.display(digits=digits, as_percent=as_percent)


def resolve_event_time(rows: List[Dict[str, Any]], args: argparse.Namespace):
    """
    决定事件列的语义：
      - 显式给了 --event-time → 用它（如果解析全失败，metrics 会拒绝输出 0%）
      - 没给但 --event 看起来像时间列 → 当时间列用
      - 否则 → 用「有值即命中」的存在语义（例如 --event 核销状态）

    返回 (event_time_key, semantics, note)
    """
    if args.event_time:
        return args.event_time, "时间列（由 --event-time 指定）", ""
    ok, parsed, non_missing, failures = M.looks_like_time_column(rows, args.event)
    if ok:
        return (args.event, "时间列（自动识别）",
                "已自动把 `%s` 当作时间列（%d/%d 可解析）" % (args.event, parsed, non_missing))
    if non_missing == 0:
        return (None, "存在语义（该列全部为空，命中数必然为 0 —— 请确认列名是否正确）",
                "⚠️ 事件列 `%s` 全部为空：命中数会是 0，请先确认列名。" % args.event)
    return (None, "存在语义（有值即命中，非时间列）",
            "⚠️ 列 `%s` 不像时间列（%d/%d 可解析，样例：%s），"
            "已改用『有值即命中』语义。如果这列其实是时间列，请显式传 --event-time。"
            % (args.event, parsed, non_missing, "、".join(failures[:3])))


def build_report(rows: List[Dict[str, Any]], args: argparse.Namespace,
                 event_time: Optional[str] = None,
                 semantics: str = "",
                 semantics_note: str = "") -> str:
    t0 = args.t0
    event = args.event
    dedupe = args.dedupe

    L: List[str] = []
    L.append("# 同批次 · 同观察年龄的核销／退款分析")
    L.append("")
    L.append("> 本报告的数字全部由 `metrics.cohort_rate` 程序产出，"
             "未使用任何心算或估计。")
    L.append("")
    L.append("## 1. 数据范围与口径")
    L.append("")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    L.append("| 来源文件 | `%s` |" % args.file)
    L.append("| 表内行数 | %d 行 |" % len(rows))
    L.append("| 支付时间字段 t0 | `%s` |" % t0)
    L.append("| 事件字段 | `%s` |" % event)
    L.append("| 事件时间列 | %s |" % ("`%s`" % event_time if event_time else "（无，按存在语义）"))
    L.append("| 命中语义 | %s |" % (semantics or "未判定"))
    L.append("| 去重键 | `%s` |" % (dedupe or "（未指定，按行计——一单多券时会算错）"))
    L.append("| 观察截止时间 T | `%s` |" % args.cutoff)
    L.append("| 观察时长 H | %g 小时 |" % args.hours)
    L.append("")
    if semantics_note:
        L.append(semantics_note)
        L.append("")
    if not dedupe:
        L.append("⚠️ **未指定 `--dedupe`**：核销表常常一行一券，一单多券时"
                 "行数不等于订单数，比例会被高估。请用 `--dedupe 订单ID`。")
        L.append("")

    # ---- 单窗口 ----
    r = M.cohort_rate(rows, t0_key=t0, event_key=event, cutoff=args.cutoff,
                      hours=args.hours, event_time_key=event_time,
                      dedupe_key=dedupe)
    L.append("## 2. H 窗口比例")
    L.append("")
    L.append("**成熟条件**：`%s + %g 小时 <= %s`" % (t0, args.hours, args.cutoff))
    L.append("")
    L.append("| 项 | 数值 |")
    L.append("|---|---|")
    L.append("| 满足成熟条件的去重对象数（分母） | %s |"
             % r.meta.get("n_eligible"))
    L.append("| 其中 H 内发生事件的去重对象数（分子） | %s |"
             % r.meta.get("n_hit"))
    L.append("| **%g 小时窗口比例** | **%s** |" % (args.hours, _fmt(r)))
    L.append("| 不足 %g 小时的对象数（单独报告，不进入分母） | %s |"
             % (args.hours, r.meta.get("n_immature")))
    L.append("| 时间无法解析的对象数（已计数，未静默丢弃） | %s |"
             % r.meta.get("n_unparsable"))
    L.append("")
    L.append("公式：" + (r.formula or ""))
    L.append("")
    if r.reason:
        L.append("**不可计算原因**：%s" % r.reason)
        L.append("")
    if r.note:
        L.append("限制：%s" % r.note)
        L.append("")

    # ---- 多节点曲线 ----
    if args.curve and event_time is None:
        L.append("## 3. 同一成熟批次的观察节点曲线")
        L.append("")
        L.append("⛔ **未生成**：没有可用的事件时间列（当前事件列 `%s` 被判为『%s』）。"
                 "时间曲线必须有真正的时间列，请显式传 `--event-time`。" % (event, semantics))
        L.append("")
    elif args.curve:
        hours_list = [float(x) for x in str(args.curve).replace("，", ",").split(",")
                      if x.strip()]
        curve = M.cohort_curve(rows, t0_key=t0, event_time_key=event_time,
                               cutoff=args.cutoff, hours_list=hours_list,
                               dedupe_key=dedupe)
        L.append("## 3. 同一成熟批次的观察节点曲线")
        L.append("")
        L.append("> 各节点分母相同（都取自满足最大窗口 %g 小时的同一批对象），"
                 "因此**相邻节点相减**得到的才是该批次的真实增量。"
                 % (max(hours_list) if hours_list else 0))
        L.append("")
        L.append("| 观察节点 | 分子 | 分母 | 比例 | 相对上一节点的增量（百分点） |")
        L.append("|---|---|---|---|---|")
        prev: Optional[Decimal] = None
        for c in curve:
            h = c.meta.get("hours")
            val = c.value if c.ok else None
            inc = ""
            if prev is not None and val is not None:
                inc = "%.2f" % (float(val - prev) * 100.0)
            if val is not None:
                prev = val
            L.append("| %g 小时 | %s | %s | %s | %s |"
                     % (h, c.meta.get("n_hit"), c.meta.get("n_cohort"),
                        _fmt(c), inc))
        L.append("")
        L.append("⛔ 禁止：把**不同购买批次**的 24 小时率与 72 小时率相减，"
                 "声称得到「第 2—3 天新增核销率」。本表各节点分母相同，才允许相减。")
        L.append("")

    # ---- 禁止事项 ----
    L.append("## 4. 本结果不能回答的问题")
    L.append("")
    L.append("- ❌ 不能回答「所有顾客通常多久核销」：本表只在"
             "**已观察到事件的对象**中统计时间，属于条件统计。")
    L.append("- ❌ 不能把「截至目前未核销」当作「永远不会核销」。")
    L.append("- ❌ 不足 %g 小时的对象共 %s 个，**不参与**上面的比例，"
             "也不能拿它们与成熟对象比较。"
             % (args.hours, r.meta.get("n_immature")))
    L.append("- ❌ 若 T 取自「导出时间」而文件只有最新快照，"
             "则无法完全还原历史状态；需检查是否有撤销／退款／状态变更记录。")
    L.append("")
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="同批次同观察年龄的核销／退款分析（成熟条件 t0 + H <= T 由程序把关）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python3 scripts/cohort.py --file 订单表.csv --t0 支付时间 \\\n"
               "      --event 核销时间 --cutoff \"2026-09-10 12:00:00\" \\\n"
               "      --hours 24 --dedupe 订单ID\n")
    p.add_argument("--file", required=True, help="数据文件（csv/tsv/xlsx）")
    p.add_argument("--sheet", default=None, help="xlsx 的工作表名")
    p.add_argument("--t0", required=True, help="支付时间列名")
    p.add_argument("--event", required=True, help="事件列名（核销时间／退款时间／核销状态）")
    p.add_argument("--event-time", dest="event_time", default=None,
                   help="事件时间列名；默认与 --event 相同")
    p.add_argument("--cutoff", required=True,
                   help="可靠的数据观察截止时间 T，例如 \"2026-09-10 12:00:00\"")
    p.add_argument("--hours", type=float, default=24.0, help="观察时长 H（小时），默认 24")
    p.add_argument("--dedupe", default=None, help="去重键，例如 订单ID 或 券码")
    p.add_argument("--curve", default=None,
                   help="多节点分析，如 1,6,24,72,168（各节点取自同一成熟批次）")
    p.add_argument("--json", action="store_true", help="输出 JSON 而不是 Markdown")
    args = p.parse_args(argv)

    if load_table is None:
        sys.stderr.write("缺少 scripts/contract.py，无法读取文件。\n")
        return 2
    try:
        t = load_table(args.file, sheet=args.sheet)
    except Exception as exc:
        sys.stderr.write("读取失败：%s\n" % exc)
        return 2

    rows = table_to_dicts(t)
    headers = list(getattr(t, "headers", []) or [])
    for need in (args.t0, args.event, args.event_time):
        if need and need not in headers:
            sys.stderr.write("找不到列 %r。现有列：%s\n" % (need, "、".join(headers)))
            return 2

    event_time, semantics, semantics_note = resolve_event_time(rows, args)

    # 明确要求了 --curve 就必须真的产出曲线；拿不出时间列时**大声失败**，
    # 而不是安静地给出一张空表（安静给出 0% 正是本技能最想消灭的东西）。
    if args.curve and event_time is None:
        sys.stderr.write(
            "--curve 需要真正的事件时间列；当前 `%s` 被判为『%s』。\n"
            "请显式传 --event-time <时间列名>，或去掉 --curve。\n"
            % (args.event, semantics))
        return 2

    if args.json:
        r = M.cohort_rate(rows, t0_key=args.t0, event_key=args.event,
                          cutoff=args.cutoff, hours=args.hours,
                          event_time_key=event_time,
                          dedupe_key=args.dedupe)
        out: Dict[str, Any] = {"rate": r.to_dict(),
                               "event_time_key": event_time,
                               "hit_semantics": semantics}
        if args.curve:
            hours_list = [float(x) for x in str(args.curve).replace("，", ",").split(",")
                          if x.strip()]
            out["curve"] = [c.to_dict() for c in M.cohort_curve(
                rows, t0_key=args.t0, event_time_key=event_time,
                cutoff=args.cutoff, hours_list=hours_list, dedupe_key=args.dedupe)]
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(build_report(rows, args, event_time=event_time,
                           semantics=semantics, semantics_note=semantics_note))
    return 0


if __name__ == "__main__":
    sys.exit(main())
