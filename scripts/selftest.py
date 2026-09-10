#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest.py —— 规范第二部分的 8 个自检案例，**可执行版**

为什么要有这个文件：
    光把"自检题"写在提示词里，普通模型会读一遍、点头、然后照样算错。
    把它们写成带断言的程序，模型就必须让**代码**给出答案；
    算错时程序会以非零退出码失败，而不是产出一份看起来很完整的报告。

每个案例都包含三件事：
    1. 正确算法（调用 metrics.py / reconcile.py）；
    2. "常见错误做法"的对照值——程序会显式证明它错在哪；
    3. 该案例要教的口径规则。

用法：
    python3 scripts/selftest.py            # 人读版
    python3 scripts/selftest.py --json     # 机器读版
退出码：全部通过 = 0；有任何一条失败 = 1。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import metrics as M  # noqa: E402

try:  # reconcile.py 由本仓库提供；缺了也不影响前 8 个案例的独立核算
    import reconcile as R  # type: ignore  # noqa: E402
except Exception:  # pragma: no cover
    R = None


# --------------------------------------------------------------------------
# 极简断言框架
# --------------------------------------------------------------------------

class Check(object):
    def __init__(self, desc: str, passed: bool, got: Any = "",
                 expect: Any = "", rule: str = "") -> None:
        self.desc = desc
        self.passed = bool(passed)
        self.got = got
        self.expect = expect
        self.rule = rule

    def to_dict(self) -> Dict[str, Any]:
        return {"desc": self.desc, "passed": self.passed,
                "got": str(self.got), "expect": str(self.expect),
                "rule": self.rule}


class Case(object):
    def __init__(self, cid: int, title: str, question: str) -> None:
        self.id = cid
        self.title = title
        self.question = question
        self.checks: List[Check] = []
        self.formula: List[str] = []
        self.wrong_way: List[str] = []
        self.conclusion: str = ""

    def check(self, desc: str, passed: bool, got: Any = "", expect: Any = "",
              rule: str = "") -> bool:
        self.checks.append(Check(desc, passed, got, expect, rule))
        return bool(passed)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "title": self.title, "question": self.question,
                "passed": self.passed, "conclusion": self.conclusion,
                "formula": self.formula, "wrong_way": self.wrong_way,
                "checks": [c.to_dict() for c in self.checks]}


def _dec(x: Any) -> Optional[Decimal]:
    return M.to_decimal(x)


class _DictTable(object):
    """
    把 [{"订单ID": "O1", ...}, ...] 包成 reconcile.py 认得的鸭子类型表。
    只用于自检题里的内存小样本；真实数据请走 contract.load_table。
    """

    def __init__(self, name: str, rows: List[Dict[str, Any]]) -> None:
        headers: List[str] = []
        for r in rows:
            for k in r.keys():
                if k not in headers:
                    headers.append(k)
        self.name = name
        self.sheet = None
        self.headers = headers
        self.rows = [[("" if r.get(h) is None else str(r.get(h, ""))) for h in headers]
                     for r in rows]
        self.source = "<内存自检样本>"
        self.n_rows = len(self.rows)
        self.n_cols = len(self.headers)


# --------------------------------------------------------------------------
# 案例 1：总体 ROI
# --------------------------------------------------------------------------

def case1() -> Case:
    c = Case(1, "总体ROI",
             "A：消耗100元，ROI为3。B：消耗10000元，ROI为1。总体ROI是多少？")
    groups = [{"name": "A", "spend": 100, "roi": 3},
              {"name": "B", "spend": 10000, "roi": 1}]

    pooled = M.pooled_roi(groups)
    naive = M.mean([3, 1])  # 错误做法：直接平均各组 ROI

    c.check("总体ROI = (100×3 + 10000×1) / (100 + 10000)",
            pooled.ok and str(pooled.value.quantize(Decimal("0.0001"))) == "1.0198",
            None if not pooled.ok else pooled.value.quantize(Decimal("0.0001")),
            "1.0198")
    c.check("分子 = 10300 元成交金额",
            pooled.numerator == Decimal("10300"), pooled.numerator, "10300")
    c.check("分母 = 10100 元消耗",
            pooled.denominator == Decimal("10100"), pooled.denominator, "10100")
    c.check("错误做法（各组ROI算术平均）确实等于 2，必须禁止",
            naive.ok and naive.value == Decimal("2"), naive.value, "2",
            "总体ROI = Σ各组成交金额 / Σ各组消耗")
    c.check("总体ROI 不等于 2（证明平均百分比是错的）",
            pooled.ok and pooled.value != Decimal("2"), pooled.value, "≠ 2")
    c.check("消耗为 0 时返回不可计算，而不是 0",
            not M.pooled_roi([{"spend": 0, "roi": 3}]).ok,
            M.pooled_roi([{"spend": 0, "roi": 3}]).reason, "不可计算")

    c.formula = ["总体ROI = 各组对应成交金额合计 / 各组对应消耗合计",
                 "总体比例 = 各组分子合计 / 各组分母合计"]
    c.wrong_way = ["(3 + 1) / 2 = 2",
                   "低估了大消耗组的权重：B 花了 99% 的钱却只影响一半的结论"]
    c.conclusion = "总体 ROI ≈ 1.0198，不是 2。加权必须回到金额本身。"
    return c


# --------------------------------------------------------------------------
# 案例 2：一单多券
# --------------------------------------------------------------------------

def case2() -> Case:
    c = Case(2, "一单多券",
             "订单表 1 笔订单支付 200 元买 2 张券；核销表 2 行，每行核销 100 元，"
             "每行『购买数量』都写 2。订单数、核销券数、核销金额各是多少？")
    orders = [{"订单ID": "O1", "订单实收金额": "200", "购买数量": "2"}]
    coupons = [{"券码": "C1", "订单ID": "O1", "核销金额": "100", "购买数量": "2"},
               {"券码": "C2", "订单ID": "O1", "核销金额": "100", "购买数量": "2"}]

    order_count = M.dedupe_count(orders, "订单ID")["distinct"]
    coupon_count = M.dedupe_count(coupons, "券码")["distinct"]
    redeem_sum = sum((_dec(r["核销金额"]) or Decimal(0)) for r in coupons)

    # 错误做法一：把每行"购买数量"相加
    qty_sum = sum((_dec(r["购买数量"]) or Decimal(0)) for r in coupons)
    # 错误做法二：直接 join，把左表金额按右表行数重复累计
    joined_rows = [dict(o, **cp) for o in orders for cp in coupons
                   if o["订单ID"] == cp["订单ID"]]
    naive_order_amount = sum((_dec(r["订单实收金额"]) or Decimal(0))
                             for r in joined_rows)

    c.check("订单数（按订单ID去重）= 1", order_count == 1, order_count, 1)
    c.check("核销券数（按券码去重）= 2", coupon_count == 2, coupon_count, 2)
    c.check("核销金额合计 = 200 元", redeem_sum == Decimal("200"), redeem_sum, 200)
    c.check("每行『购买数量』相加 = 4，因此它绝不能当作核销券数",
            qty_sum == Decimal("4") and qty_sum != Decimal(coupon_count),
            qty_sum, "4（错误值）",
            "核销行里的『购买数量』可能是原订单购买总数，不能逐行相加")
    c.check("直接 join 后订单实收金额被累计成 400，确实是错的",
            naive_order_amount == Decimal("400")
            and naive_order_amount != Decimal("200"),
            naive_order_amount, "400（错误值）")
    c.check("正确顺序：先按订单ID汇总核销金额，再与订单表 1:1 关联",
            redeem_sum == _dec(orders[0]["订单实收金额"]) == Decimal("200"),
            "核销金额 200 = 订单实收 200", "相等")

    # 若 reconcile.py 可用，用第二种路径独立复核（规范第十三节要求换一条汇总路径）
    if R is not None:
        try:
            agg = R.aggregate_then_join(
                _DictTable("订单表", orders), _DictTable("核销表", coupons),
                left_key="订单ID", right_key="订单ID",
                right_agg={"核销金额": "sum", "购买数量": "max"},
                left_value_cols=["订单实收金额"], right_value_cols=["核销金额"])
            verdict = str(agg.get("verdict", ""))
            amplified = bool(agg.get("amplified"))
            c.check("reconcile.aggregate_then_join 独立复核：不放大、判定通过",
                    (not amplified) and ("失败" not in verdict),
                    "verdict=%s amplified=%s" % (verdict, amplified),
                    "通过 / 不放大")
            # 反向验证：不聚合直接 join 必须被判失败（证明护栏真的会拦住错误做法）
            naive = R.join_audit(_DictTable("订单表", orders),
                                 _DictTable("核销表", coupons),
                                 "订单ID", "订单ID",
                                 left_value_cols=["订单实收金额"],
                                 right_value_cols=["核销金额"])
            c.check("反向验证：直接 join 会被判『失败-禁止使用该结果』",
                    "失败" in str(naive.get("verdict", "")),
                    "verdict=%s" % naive.get("verdict"), "含『失败』")
        except Exception as exc:  # pragma: no cover
            c.check("reconcile.aggregate_then_join 可调用", False,
                    "异常：%s" % exc, "正常返回")

    c.formula = ["订单数 = COUNT(DISTINCT 订单ID)",
                 "核销券数 = COUNT(DISTINCT 券码)",
                 "同一订单的核销金额先按订单汇总，再关联订单表"]
    c.wrong_way = ["2 笔订单（把核销行当成订单）",
                   "4 张券（把每行『购买数量』=2 相加）",
                   "400 元（join 后把左表金额按右表行数重复累计）"]
    c.conclusion = "1 笔订单、2 张核销券、核销金额 200 元。"
    return c


# --------------------------------------------------------------------------
# 案例 3：观察窗口（成熟条件）
# --------------------------------------------------------------------------

def case3() -> Case:
    T = datetime(2026, 9, 10, 12, 0, 0)  # 可靠的数据观察截止时间
    c = Case(3, "观察窗口",
             "截止今天有 3 笔订单：A 已支付 5 天且 24 小时内核销；"
             "B 已支付 4 天尚未核销；C 只支付了 3 小时尚未核销。24 小时核销比例？")
    rows = [
        {"订单ID": "A", "支付时间": (T - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"),
         "核销时间": (T - timedelta(days=5) + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")},
        {"订单ID": "B", "支付时间": (T - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S"),
         "核销时间": ""},
        {"订单ID": "C", "支付时间": (T - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"),
         "核销时间": ""},
    ]
    r = M.cohort_rate(rows, t0_key="支付时间", event_key="核销时间",
                      cutoff=T, hours=24, dedupe_key="订单ID")
    naive = M.safe_div(1, 3)  # 错误做法：把未满 24h 的 C 也算进分母

    c.check("满足成熟条件的订单数（t0 + 24h ≤ T）= 2",
            r.meta.get("n_eligible") == 2, r.meta.get("n_eligible"), 2)
    c.check("不足 24 小时的订单 = 1（C），单独报告",
            r.meta.get("n_immature") == 1, r.meta.get("n_immature"), 1)
    c.check("24 小时核销比例 = 1/2 = 50%",
            r.ok and str(r.value) == "0.5",
            None if not r.ok else r.value, "0.5")
    c.check("错误做法 1/3 ≈ 33.3% 确实不同，必须禁止",
            naive.ok and r.ok and naive.value != r.value,
            naive.value, "0.3333（错误值）")
    c.check("报告里带出了未成熟批次的提示", "不足" in (r.note or ""),
            (r.note or "")[:40] + "…", "含未成熟提示")

    # 同一批次多节点观察：允许相减；不同批次相减则不允许
    rows2 = list(rows) + [
        {"订单ID": "D", "支付时间": (T - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),
         "核销时间": (T - timedelta(days=30) + timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")},
    ]
    curve = M.cohort_curve(rows2, t0_key="支付时间", event_time_key="核销时间",
                           cutoff=T, hours_list=[24, 72], dedupe_key="订单ID")
    ok_curve = len(curve) == 2 and curve[0].meta["n_cohort"] == curve[1].meta["n_cohort"]
    c.check("多节点曲线基于同一成熟批次（各节点分母相同，可相减得增量）",
            ok_curve, [x.meta.get("n_cohort") for x in curve], "两个节点分母相同")

    c.formula = ["t0 = 每笔订单的支付时间；T = 可靠的数据观察截止时间；H = 观察时长",
                 "可进入 H 窗口分析的条件：t0 + H ≤ T",
                 "H窗口核销订单比例 = 成熟且H内至少一次有效核销的订单数 / 成熟订单数"]
    c.wrong_way = ["1/3 = 33.3%（把只支付 3 小时的订单也算进分母）",
                   "把今天核销额 ÷ 今天成交额 称作真实核销率",
                   "用新订单的 1 天结果与老订单的 9 天结果直接排名"]
    c.conclusion = "只有 A、B 满 24 小时窗口，所以是 1/2 = 50%，不是 1/3。"
    return c


# --------------------------------------------------------------------------
# 案例 4：比例变化
# --------------------------------------------------------------------------

def case4() -> Case:
    c = Case(4, "比例变化", "CTR 从 4% 到 5%，怎么描述变化？")
    pp = M.pct_point_delta(0.05, 0.04)
    rel = M.relative_change(0.05, 0.04)
    wrong = M.relative_change(0.05, 0.04)  # 相对变化 25%，不是 1%

    c.check("百分点变化 = 1 个百分点",
            pp.ok and pp.value == Decimal("0.01"), pp.value, "0.01 个百分点")
    c.check("相对变化 = 25%",
            rel.ok and rel.value == Decimal("0.25"), rel.value, "0.25")
    c.check("『提升 1%』是错的：相对变化是 25%，不是 1%",
            not (rel.value == Decimal("0.01")), "25% ≠ 1%", "25%")
    c.check("绝对量（百分点）与相对量必须分别报告",
            pp.unit == "个百分点" and wrong.value is not None,
            pp.unit, "个百分点")

    bp = M.relative_change(5, 0)
    c.check("基期为 0 时不输出普通增长率",
            not bp.ok, bp.reason, "不可计算")

    c.formula = ["百分点变化 = 本期比例 － 基期比例",
                 "相对变化 = (本期－基期) / 基期（基期为 0 时不输出）"]
    c.wrong_way = ["说成『提升 1%』（把百分点当成百分比）",
                   "基期为 0 时硬算增长率，得出 ∞ 或 100%"]
    c.conclusion = "CTR 从 4% 到 5%：提高 1 个百分点，相对提升 25%。"
    return c


# --------------------------------------------------------------------------
# 案例 5：广告归因
# --------------------------------------------------------------------------

def case5() -> Case:
    c = Case(5, "广告归因",
             "投放前日均卖 3 万元；投放两天合计卖 6 万元、消耗 400 元。"
             "能否说广告新增 ROI = 150？")
    naive = M.safe_div(60000, 400)  # 错误做法
    guard_bare = M.causal_increment_guard()
    guard_attr = M.causal_increment_guard(has_attribution=True)
    guard_obs = M.causal_increment_guard(has_attribution=True,
                                         has_control_or_preperiod=True)
    guard_rct = M.causal_increment_guard(has_attribution=True,
                                        has_control_or_preperiod=True,
                                        has_random_assignment=True)

    c.check("60000/400 = 150 这个除法本身能算出来（所以它才危险）",
            naive.ok and naive.value == Decimal("150"), naive.value, "150")
    c.check("证据标签必须是『数据不足』，不能称 150 为广告新增ROI",
            guard_bare["evidence_label"] == "数据不足",
            guard_bare["evidence_label"], "数据不足")
    c.check("只有平台归因数据时，只能说归因、不能说新增",
            guard_attr["evidence_label"] == "待验证假设",
            guard_attr["evidence_label"], "待验证假设")
    c.check("有前后对照/控制组仍只能说对照观察，不能称因果",
            guard_obs["evidence_label"] == "观察性差异",
            guard_obs["evidence_label"], "观察性差异")
    c.check("有随机分配才能主张因果",
            guard_rct["evidence_label"] == "已对账事实",
            guard_rct["evidence_label"], "已对账事实")
    c.check("两天的总成交甚至不足以证明投放让销量上升",
            "无投放比较" in guard_bare["detail"] or "比较" in guard_bare["detail"],
            guard_bare["detail"], "含『缺少可信的无投放比较结果』")

    c.formula = ["平台归因ROI = 平台归因成交金额 / 对应广告消耗（须说明归因模型与时间窗口）",
                 "新增成交需要可信实验或有效对照，不能由总成交 ÷ 消耗 得出"]
    c.wrong_way = ["60000 / 400 = 150，然后声称广告新增 ROI 为 150",
                   "把『平台归因成交』直接当成『广告新增成交』",
                   "用投放前后对比当作随机实验结果"]
    c.conclusion = ("缺少广告归因数据和可信的无投放比较结果 —— 这一条只能标为『数据不足』，"
                    "不能给出新增 ROI。")
    return c


# --------------------------------------------------------------------------
# 案例 6：未匹配核销
# --------------------------------------------------------------------------

def case6() -> Case:
    c = Case(6, "未匹配核销",
             "9 月核销 100 张券，其中 30 张在 8 月购买。"
             "分析 9 月购买批次时，核销分子是多少？分析 9 月门店接待量时呢？")
    # 9 月核销记录 100 行：30 行属于 8 月购买批次，70 行属于 9 月购买批次
    sept_redeem = ([{"券码": "A%03d" % i, "购买批次": "8月", "核销月份": "9月"}
                    for i in range(30)] +
                   [{"券码": "B%03d" % i, "购买批次": "9月", "核销月份": "9月"}
                    for i in range(70)])
    cohorts = M.mature_cohort(sept_redeem, t0_key="购买批次",
                              cutoff="2026-10-01", hours=0)
    # 上面这行只用于演示时间解析；真正的批次过滤按业务口径做：
    sept_purchase_in_sept_redeem = [r for r in sept_redeem if r["购买批次"] == "9月"]
    numerator_cohort = len(sept_purchase_in_sept_redeem)          # 70
    numerator_store_visits = len(sept_redeem)                     # 100
    purchased_in_sept = 200                                       # 9 月购买券总数（假设）

    wrong = M.redeem_coupon_ratio(100, purchased_in_sept)         # 错误做法
    right = M.redeem_coupon_ratio(numerator_cohort, purchased_in_sept)

    c.check("9 月购买批次的核销分子 = 70（不是 100）",
            numerator_cohort == 70, numerator_cohort, 70)
    c.check("9 月门店接待量口径 = 100 张有效核销（可以计入 8 月购买的券）",
            numerator_store_visits == 100, numerator_store_visits, 100)
    c.check("错误做法 100/200 = 50% 与正确做法 70/200 = 35% 不同",
            wrong.ok and right.ok and wrong.value != right.value,
            "错误 %.2f%% / 正确 %.2f%%" % (float(wrong.value) * 100, float(right.value) * 100),
            "必须区分")
    c.check("按购买批次算核销率时，分子分母必须同批次",
            right.ok and str(right.value) == "0.35", right.value, "0.35")

    c.formula = ["批次核销率：分子与分母必须来自同一购买批次",
                 "门店接待量：按有效核销记录计，不受购买批次限制",
                 "两种口径回答的是两个不同问题，不能混用同一组数字"]
    c.wrong_way = ["把 30 张 8 月购买的券计入 9 月批次的核销分子",
                   "用同一张核销表同时回答批次核销率和门店接待量，却不说明口径切换"]
    c.conclusion = ("分析 9 月购买批次 → 分子 70；分析 9 月门店接待量 → 100 张。"
                    "离开口径谈核销率没有意义。")
    return c


# --------------------------------------------------------------------------
# 案例 7：退款口径
# --------------------------------------------------------------------------

def case7() -> Case:
    c = Case(7, "退款口径",
             "某订单用户实付 98 元、订单实收 100 元、退款 99 元。"
             "能否说这笔订单亏了 1 元？")
    paid = _dec("98")
    received = _dec("100")
    refund = _dec("99")
    diff = paid - refund                      # -1
    gap = received - paid                     # 2（优惠/补贴口径差异）
    g0 = M.net_revenue_guard()
    g1 = M.net_revenue_guard(has_reconciliation=True)
    g2 = M.net_revenue_guard(has_reconciliation=True,
                             has_fee_and_subsidy_policy=True)
    g3 = M.net_revenue_guard(has_reconciliation=True,
                             has_fee_and_subsidy_policy=True,
                             has_fulfillment_cost=True)

    c.check("98 － 99 = －1 这个减法是能算的（所以它才危险）",
            diff == Decimal("-1"), diff, "-1")
    c.check("实收 － 实付 = 2 元，说明存在优惠/补贴口径差异，必须先核对",
            gap == Decimal("2"), gap, "2")
    c.check("未对账时证据标签 = 数据不足，不得称亏损",
            g0["evidence_label"] == "数据不足", g0["evidence_label"], "数据不足")
    c.check("已对账但缺补贴政策，仍不能说亏损",
            g1["evidence_label"] == "数据不足", g1["evidence_label"], "数据不足")
    c.check("已对账+有费用补贴口径 → 可谈净收入",
            g2["verdict"].startswith("可谈净收入"), g2["verdict"], "可谈净收入")
    c.check("只有补上履约成本才能谈利润",
            g3["can_profit"] is True and g2["can_profit"] is False,
            "g2.can_profit=%s / g3.can_profit=%s" % (g2["can_profit"], g3["can_profit"]),
            "False / True")
    c.check("缺失的退款金额用『-』表示时必须返回 None 而不是 0",
            M.to_decimal("-") is None and M.to_decimal("0") == Decimal("0"),
            "to_decimal('-')=%s" % M.to_decimal("-"), "None")

    c.formula = ["金额必须区分：用户实付 / 订单实收 / 预计收入 / 退款 / 结算收入 / 利润",
                 "净收入与利润均需对账；缺履约成本、服务费、补贴处理时不计算利润"]
    c.wrong_way = ["直接把 98 － 99 = －1 元当成经营亏损",
                   "把『用户实付－退款』或『订单实收－退款』自动称为净收入",
                   "把退款金额字段的『-』当成 0 参与合计"]
    c.conclusion = ("实付 98、实收 100 说明口径本身不止一套；"
                    "在补齐优惠、补贴、退款口径与前费用之前，这笔单只能说『数据不足』。")
    return c


# --------------------------------------------------------------------------
# 案例 8：样本单位
# --------------------------------------------------------------------------

def case8() -> Case:
    c = Case(8, "样本单位",
             "有 3000 笔订单，但只有 2 天投放记录。能否说 3000 个独立样本证明广告有效？")
    g = M.independent_units_guard(n_records=3000, n_periods=2, unit_kind="投放天数")
    g2 = M.independent_units_guard(n_records=3000, n_periods=30,
                                   unit_kind="投放天数")
    mc = M.multiple_comparison_guard(57)

    c.check("独立单位数 = 2（天），不是 3000",
            g["n_independent_units"] == 2, g["n_independent_units"], 2)
    c.check("记录数不能当作独立样本数",
            g["record_level_n_usable"] is False, g["record_level_n_usable"], False)
    c.check("证据标签 = 数据不足",
            g["evidence_label"] == "数据不足", g["evidence_label"], "数据不足")
    c.check("不允许做统计推断（独立单位 < 10）",
            g["ok_to_infer"] is False, g["ok_to_infer"], False)
    c.check("30 天 + 3000 单：不可用 n=3000，但独立单位够了",
            g2["ok_to_infer"] is True and g2["record_level_n_usable"] is False,
            "ok=%s record_level=%s" % (g2["ok_to_infer"], g2["record_level_n_usable"]),
            "True / False")
    c.check("57 个门店同时比较 → 只能标注为探索性筛查",
            "探索性" in mc["verdict"], mc["verdict"], "探索性筛查")
    c.check("禁止使用固定门槛（1000曝光/30样本/7天）",
            "固定门槛" in g["note"], g["note"][:30] + "…", "含固定门槛禁令")
    c.check("2 笔样本的 Wilson 区间不可用于 ROI／客单价",
            "不要用于 ROI" in (M.wilson_interval(1, 2).note or "") or
            "不能弥补" in (M.wilson_interval(1, 2).note or ""),
            (M.wilson_interval(1, 2).note or "")[:30] + "…", "含限制说明")

    c.formula = ["样本量必须按研究问题确定：研究订单金额以订单为单位；"
                 "研究用户转化以用户为单位；研究投放效果以日期或实验组为单位",
                 "只有符合独立二项试验假设时，才使用普通比例区间"]
    c.wrong_way = ["声称有 3000 个独立样本证明广告有效",
                   "套用固定门槛：1000 曝光一定够 / 30 个样本就可靠 / 7 天一定可以下结论",
                   "用两个置信区间是否重叠代替正式的差异比较"]
    c.conclusion = ("3000 笔订单 ≠ 3000 个独立样本。这里真正的独立单位是 2 天，"
                    "不足以做任何统计推断。")
    return c


ALL_CASES = [case1, case2, case3, case4, case5, case6, case7, case8]


# --------------------------------------------------------------------------
# 运行与渲染
# --------------------------------------------------------------------------

def run_all() -> List[Case]:
    out: List[Case] = []
    for fn in ALL_CASES:
        try:
            out.append(fn())
        except Exception as exc:  # 单个案例崩了也要报出来，不能静默跳过
            c = Case(int(fn.__name__[4:]), fn.__doc__ or fn.__name__, "")
            c.check("案例执行未抛异常", False, "异常：%r" % (exc,), "正常返回")
            out.append(c)
    return out


def render(cases: List[Case], verbose: bool = True) -> str:
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("抖音来客／本地推 分析前自检题 · 可执行版（规范第二部分 8 个案例）")
    lines.append("=" * 78)
    if R is None:
        lines.append("提示：未导入 reconcile.py，案例 2 的第二种复核路径已跳过。")
    lines.append("")
    for c in cases:
        mark = "PASS" if c.passed else "FAIL"
        lines.append("【案例%d】%s ............ %s" % (c.id, c.title, mark))
        lines.append("  问题：" + c.question)
        for chk in c.checks:
            flag = "  ✓" if chk.passed else "  ✗"
            lines.append("%s %s" % (flag, chk.desc))
            lines.append("      实得：%s ｜ 期望：%s" % (chk.got, chk.expect))
            if chk.rule:
                lines.append("      依据：%s" % chk.rule)
        if verbose:
            lines.append("  正确公式：")
            for f in c.formula:
                lines.append("      - %s" % f)
            lines.append("  常见错误做法（本技能禁止）：")
            for w in c.wrong_way:
                lines.append("      - %s" % w)
            lines.append("  结论：%s" % c.conclusion)
        lines.append("")
    total = len(cases)
    passed = sum(1 for c in cases if c.passed)
    nchecks = sum(len(c.checks) for c in cases)
    npass = sum(1 for c in cases for chk in c.checks if chk.passed)
    lines.append("-" * 78)
    lines.append("案例：%d/%d 通过 ｜ 断言：%d/%d 通过" % (passed, total, npass, nchecks))
    if passed == total:
        lines.append("自检通过：可以开始分析真实数据。")
        lines.append("注意：自检只证明**规则被正确实现**，不证明你的数据口径是对的。"
                     "下一步仍必须先做数据契约体检。")
    else:
        lines.append("自检未通过：先修正方法，再分析真实数据。")
    lines.append("-" * 78)
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="规范自检题的可执行版：先让程序把方法验对，再碰真实数据。")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.add_argument("--quiet", action="store_true", help="只输出结论行")
    args = p.parse_args(argv)

    cases = run_all()
    if args.json:
        print(json.dumps({"cases": [c.to_dict() for c in cases],
                          "all_passed": all(c.passed for c in cases)},
                         ensure_ascii=False, indent=2))
    elif args.quiet:
        for c in cases:
            print("案例%d %s: %s" % (c.id, c.title, "PASS" if c.passed else "FAIL"))
    else:
        print(render(cases))
    return 0 if all(c.passed for c in cases) else 1


if __name__ == "__main__":
    sys.exit(main())
