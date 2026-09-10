#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report.py —— 固定输出格式的生成器（规范第十四节）

作用：
    把「报告格式」也变成程序产出，避免模型每次自由发挥：
    - 骨架固定为 7 段（核心结论／数据范围与口径／关键数字／分析／不能下的结论／下一步／统计学教学）；
    - 关键数字必须由其他脚本的 --json 注入，**不允许手抄**；
    - 自动附上 12 项核对清单与四类证据标签的图例；
    - 自动附上每条数字的来源、分母、公式、口径（可追溯）。

用法：
    python3 scripts/report.py --scaffold > 报告骨架.md
    python3 scripts/report.py --facts facts.json --out 报告.md
    python3 scripts/report.py --checklist
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

CHECKLIST_12 = [
    "金额合计能否由原始记录独立复算。",
    "日、商品、渠道等互斥完整分组能否回加总量。",
    "状态类别是否互斥；不互斥时不能强行相加。",
    "关联是否造成重复累计。",
    "每个比例的分母是否匹配。",
    "日期是否包含部分日或跨期记录。",
    "退款、核销状态是否处理撤销和后续变更。",
    "缺失是否被错误当成零。",
    "极端值是否核实。",
    "用户口述与表格结果是否混为一谈。",
    "抽样检验是否误当成全量验证。",
    "每项重要结论是否能追溯到字段、筛选和公式。",
]

EVIDENCE_LABELS = ["已对账事实", "观察性差异", "待验证假设", "数据不足"]

RED_LINES = [
    "「今天核销额 ÷ 今天成交额 = 真实核销率」",
    "「整体 ROI = 各组 ROI 的平均」",
    "「核销率 0%」（当分母为 0 时 —— 应该说「不可计算」）",
    "「广告新增 ROI = 总成交额 ÷ 消耗」",
    "「3000 个订单 = 3000 个独立样本」",
    "「提升 1%」（当实际是 1 个百分点时）",
    "「净收入 = 用户实付 － 退款」（未对账时）",
    "「剔除已退款订单后核销率提升到 XX%」",
    "「A 素材比 B 素材好」（当两者观察年龄或批次不同时）",
]


def _round_for_display(value: Any, max_dp: int = 4) -> str:
    """
    把 Decimal 字符串收成人类可读的精度：最多 4 位小数，去掉多余的尾零。
    只影响**显示**，不改动程序算出的原始值（原始值仍在 json 里）。
    """
    try:
        from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
        d = Decimal(str(value))
        if not d.is_finite():
            return str(value)
        q = d.quantize(Decimal(1).scaleb(-max_dp), rounding=ROUND_HALF_UP)
        q = q.normalize()
        s = format(q, "f")
        return s
    except (InvalidOperation, ValueError, ArithmeticError):
        return str(value)


def _fmt_num(item: Dict[str, Any]) -> str:
    """把一条数字事实渲染成可追溯的一行。"""
    label = str(item.get("label") or item.get("name") or "（未命名指标）")
    value = item.get("value")
    unit = item.get("unit") or ""
    if value is None or item.get("computable") is False:
        shown = "**不可计算**（%s）" % (item.get("reason") or "分母为 0 或数据不足")
    elif item.get("as_percent") or unit == "%":
        try:
            shown = "**%.2f%%**" % (float(value) * 100.0)
        except (TypeError, ValueError):
            shown = "**%s**" % value
    else:
        shown = "**%s**" % _round_for_display(value)
        if unit:
            shown = shown + " " + str(unit)
    num = item.get("numerator")
    den = item.get("denominator")
    if num is not None or den is not None:
        shown += "（分子 %s / 分母 %s）" % (num, den)
    bits: List[str] = []
    if item.get("source"):
        bits.append("来源：%s" % item["source"])
    if item.get("formula"):
        bits.append("公式：%s" % item["formula"])
    if item.get("scope"):
        bits.append("口径：%s" % item["scope"])
    tail = ("　｜　" + "　｜　".join(bits)) if bits else ""
    return "- %s = %s%s" % (label, shown, tail)


def load_facts(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        for key in ("facts", "numbers", "key_numbers", "results"):
            if isinstance(data.get(key), list):
                return data[key]
        # 单条 Result：{"value":..., "numerator":...}
        if "value" in data or "computable" in data:
            return [data]
        flat: List[Dict[str, Any]] = []
        for k, v in data.items():
            if isinstance(v, dict):
                item = dict(v)
                item.setdefault("label", k)
                flat.append(item)
        return flat
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    raise ValueError("facts.json 结构无法识别：期望 list 或 dict")


def scaffold(facts: Optional[List[Dict[str, Any]]] = None,
             meta: Optional[Dict[str, Any]] = None) -> str:
    meta = meta or {}
    L: List[str] = []
    L.append("# 经营数据分析报告")
    L.append("")
    L.append("> 本报告的关键数字由 `scripts/` 下的程序产出；"
             "模型只负责解释与提出假设。")
    L.append("> 每条结论自带证据标签：%s。" % "／".join(EVIDENCE_LABELS))
    L.append("> **不使用主观「可信度 87%」或任意机会分。**")
    L.append("")

    L.append("## 执行顺序（勾选后才算完成）")
    L.append("")
    L.append("- [ ] 1 开工前自检 `python3 scripts/selftest.py`（8/8 PASS）")
    L.append("- [ ] 2 数据契约体检 `python3 scripts/contract.py <文件...>`")
    L.append("- [ ] 3 口径确认（一行是什么、时间窗、状态、归因范围）")
    L.append("- [ ] 4 清洗与关联审计 `python3 scripts/reconcile.py ...`")
    L.append("- [ ] 5 计算（脚本产出）`python3 scripts/cohort.py ...` 等")
    L.append("- [ ] 6 12 项核对（见文末）")
    L.append("- [ ] 7 按固定格式输出")
    L.append("")

    L.append("## 1. 核心结论（最多 5 条，重要事实优先）")
    L.append("")
    L.append("1. 【证据标签】结论 —— 对应关键数字 ___")
    L.append("2. 【证据标签】结论 —— 对应关键数字 ___")
    L.append("3. 【证据标签】结论 —— 对应关键数字 ___")
    L.append("")
    L.append("> 超过 5 条说明你还没分清主次。宁可少说，不可凑数。")
    L.append("")

    L.append("## 2. 数据范围与口径")
    L.append("")
    L.append("| 项 | 内容 |")
    L.append("|---|---|")
    L.append("| 使用的文件与工作表 | %s |" % meta.get("files", "（待填）"))
    L.append("| 统计粒度（一行是什么） | %s |" % meta.get("grain", "（待填，需人工确认）"))
    L.append("| 业务时间范围 | %s |" % meta.get("time_range", "（待填）"))
    L.append("| 时间筛选方式 | %s |" % meta.get("time_filter", "（待填，须按业务时间字段，不用文件名）"))
    L.append("| 状态筛选 | %s |" % meta.get("status_filter", "（待填）"))
    L.append("| 排除了什么 | %s |" % meta.get("excluded", "（待填）"))
    L.append("| 状态截至何时 | %s |" % meta.get("as_of", "（待填：导出时间 ≠ 业务发生时间）"))
    L.append("| 归因模型与窗口 | %s |" % meta.get("attribution", "（待填，若无则写「未说明」）"))
    L.append("")

    L.append("## 3. 关键数字（数值 ＋ 分母 ＋ 单位 ＋ 时间 ＋ 来源）")
    L.append("")
    if facts:
        for item in facts:
            L.append(_fmt_num(item))
        L.append("")
        notes = [x.get("note") for x in facts if x.get("note")]
        if notes:
            L.append("**这些数字的限制：**")
            L.append("")
            seen = set()
            for n in notes:
                if n not in seen:
                    seen.add(n)
                    L.append("- %s" % n)
            L.append("")
    else:
        L.append("_（未注入 facts.json —— 请用 `--json` 从其他脚本导出后 `--facts` 注入，"
                 "不要手工转抄数字）_")
        L.append("")

    L.append("## 4. 分析")
    L.append("")
    L.append("### 4.1 主题一：___")
    L.append("")
    L.append("- 【数据事实】程序算出的、可复算的，注明来源与分母。")
    L.append("- 【统计判断】在数据事实之上的推断，注明不确定性与区间。")
    L.append("- 【业务假设】可能解释差异的机制，明确标注为假设。")
    L.append("- 【行动建议】带成本或风险边界的建议。")
    L.append("")
    L.append("### 4.2 主题二：___")
    L.append("")
    L.append("- 【数据事实】___")
    L.append("- 【统计判断】___")
    L.append("- 【业务假设】___")
    L.append("- 【行动建议】___")
    L.append("")

    L.append("## 5. 不能下的结论")
    L.append("")
    L.append("| 想回答的问题 | 为什么现在答不了 | 需要什么证据 |")
    L.append("|---|---|---|")
    L.append("| ___ | ___ | ___ |")
    L.append("")

    L.append("## 6. 下一步（最多 3 项）")
    L.append("")
    L.append("| # | 要做什么 | 需要的数据 | 负责人/执行对象 | 观察窗口 | 停止或复查条件 |")
    L.append("|---|---|---|---|---|---|")
    L.append("| 1 | ___ | ___ | ___ | ___ | ___ |")
    L.append("| 2 | ___ | ___ | ___ | ___ | ___ |")
    L.append("")
    L.append("> 不用没有成本依据的比例推荐预算。")
    L.append("")

    L.append("## 7. 统计学教学（用本次数据讲 1–2 个概念）")
    L.append("")
    L.append("**概念：___（例如「百分点 vs 百分比」「成熟批次」「加权平均」）**")
    L.append("")
    L.append("- 业务含义：___")
    L.append("- 本次数据里的例子：___")
    L.append("- 简单公式：`___`")
    L.append("- 常见的错误理解：___")
    L.append("")

    L.append("## 附录 A · 结论追溯表")
    L.append("")
    L.append("| 结论 | 来源文件/工作表 | 统计粒度 | 时间筛选 | 状态筛选 | 关联方法 | 分子/分母或金额字段 | 公式 | 核验结果 | 适用限制 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    L.append("| ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ | ___ |")
    L.append("")

    L.append("## 附录 B · 发布前 12 项核对")
    L.append("")
    for i, item in enumerate(CHECKLIST_12, 1):
        L.append("%d. [ ] %s" % (i, item))
    L.append("")
    L.append("> 不能用同一段错误逻辑重复运行两次，称为独立验证。")
    L.append("> 优先使用另一种汇总路径、原始总额或逐单对账。")
    L.append("> 发现对账差异时：报告差异范围、可能影响和仍可使用的结论，"
             "不要为了交付完整报告而隐藏问题。")
    L.append("")

    L.append("## 附录 C · 红线清单（本报告一句都不许出现）")
    L.append("")
    for line in RED_LINES:
        L.append("- ❌ %s" % line)
    L.append("")
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="固定输出格式的报告生成器")
    p.add_argument("--scaffold", action="store_true", help="输出空报告骨架")
    p.add_argument("--facts", default=None, help="注入 facts.json（由其他脚本 --json 产出）")
    p.add_argument("--out", default=None, help="写入文件；默认打印到标准输出")
    p.add_argument("--checklist", action="store_true", help="只打印 12 项核对清单")
    args = p.parse_args(argv)

    if args.checklist:
        for i, item in enumerate(CHECKLIST_12, 1):
            print("%d. %s" % (i, item))
        return 0

    facts = load_facts(args.facts) if args.facts else None
    text = scaffold(facts=facts)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        sys.stderr.write("已写入 %s（%d 字符）\n" % (args.out, len(text)))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
