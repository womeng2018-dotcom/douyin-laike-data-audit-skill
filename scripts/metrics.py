#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
metrics.py —— 抖音来客／本地推 经营数据统计的**公式库**

设计原则（对应规范第七～十一节）：
    1. 每一个比例都必须能回答：分子、分母、对象、时间、状态、去重方式、归因范围。
       因此本库返回的不是裸数字，而是 `Result` 对象——它把分子、分母、公式、
       口径说明、不可计算原因一起带出来，便于结论可复算、可追溯。
    2. 分母为 0 时**不返回 0**，返回"不可计算"并说明原因。
       把 0/0 显示成 0%，是本技能要消灭的头号错误。
    3. 总体比例／总体 ROI 一律"先加分子分母再加总相除"，
       绝不平均各组百分比（见自检案例 1）。
    4. 金额用 Decimal 计算，统计量用 float 计算，两者不混。
    5. 本库只做**计算与标记**，不做业务判断。
       IQR 只标记待查，绝不删除；未成熟批次只单独报告，绝不混进固定窗口比较。

命令行自检：
    python3 metrics.py selftest
"""

from __future__ import annotations

import argparse
import math
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

MISSING_TOKENS = {"", "-", "—", "–", "/", "\\", "n/a", "na", "null", "none",
                  "无", "未知", "不适用", "空", "nan"}

__all__ = [
    "Result", "to_decimal", "to_float", "is_missing",
    "safe_div", "pooled_ratio", "pooled_roi", "ratio_of_sums",
    "pct_point_delta", "relative_change", "delta_amount",
    "mean", "median", "quantile_linear", "percentile_report",
    "sample_stdev", "cv", "iqr_outliers", "head_concentration",
    "wilson_interval", "attribution_roi", "refund_order_ratio",
    "redeem_coupon_ratio",
    "mature_cohort", "cohort_rate", "cohort_curve",
    "dedupe_count", "standardize_ratio",
    "evidence_label", "EVIDENCE_LABELS", "causal_increment_guard",
    "net_revenue_guard", "independent_units_guard", "multiple_comparison_guard",
    "cases_from_spec",
]


# --------------------------------------------------------------------------
# 基础：缺失值、数值转换
# --------------------------------------------------------------------------

def is_missing(value: Any) -> bool:
    """判断一个单元格是否属于"缺失"。注意：0 不是缺失，False 不是缺失。"""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in MISSING_TOKENS
    return False


def to_decimal(value: Any,
               quantize: Optional[str] = None) -> Optional[Decimal]:
    """
    把单元格转成 Decimal。**缺失返回 None，绝不返回 0**（规范第四节第 4 条）。

    >>> to_decimal("-") is None
    True
    >>> to_decimal("¥1,234.50")
    Decimal('1234.50')
    >>> to_decimal("0")
    Decimal('0')
    """
    if is_missing(value):
        return None
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, bool):
        return None
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, float):
        # 走 str 以避免二进制浮点噪声（0.1+0.2 类问题）
        d = Decimal(repr(value))
    else:
        s = str(value).strip()
        s = s.replace(",", "").replace("，", "")
        s = s.replace("¥", "").replace("￥", "").replace("$", "")
        s = s.replace("元", "").replace("RMB", "").replace("rmb", "").strip()
        # 全角负号、括号负数写法 (123.45)
        s = s.replace("－", "-").replace("−", "-")
        if s.startswith("(") and s.endswith(")"):
            s = "-" + s[1:-1]
        if s.endswith("%"):
            inner = to_decimal(s[:-1])
            if inner is None:
                return None
            d = inner / Decimal(100)
        else:
            try:
                d = Decimal(s)
            except (InvalidOperation, ValueError):
                return None
        if not d.is_finite():
            return None
    if quantize is not None:
        d = d.quantize(Decimal(quantize), rounding=ROUND_HALF_UP)
    return d


def to_float(value: Any) -> Optional[float]:
    d = to_decimal(value)
    return None if d is None else float(d)


TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d",
    "%Y年%m月%d日 %H:%M:%S", "%Y年%m月%d日", "%Y-%m-%dT%H:%M:%S",
)
"""时间解析格式表。**解析失败必须计数并报告，不能静默丢弃**（规范第四节第 6 条）。"""


def parse_datetime(value: Any):
    """
    把单元格解析成 datetime。缺失返回 None。

    覆盖常见导出格式（含中文日期、ISO 带 T）。解析失败返回 None ——
    调用方必须自己计数并报告失败数量，本函数不抛异常、不猜。
    """
    from datetime import datetime as _dt
    if isinstance(value, _dt):
        return value
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in MISSING_TOKENS:
        return None
    for f in TIME_FORMATS:
        try:
            return _dt.strptime(s, f)
        except ValueError:
            continue
    try:
        return _dt.fromisoformat(s.replace("Z", ""))
    except Exception:
        return None


def looks_like_time_column(rows: Sequence[Dict[str, Any]], key: str,
                           sample: int = 200, threshold: float = 0.8):
    """
    判断某一列到底是「时间列」还是「状态/类别列」。

    为什么需要它：把 `订单状态` 这种列误当成时间列，会让"H 窗口核销率"
    算成 0%，而报告看起来完全正常 —— 这正是本技能要消灭的"看似完整"。
    返回 (is_time, parsed, non_missing, examples_of_failures)。
    """
    parsed = 0
    non_missing = 0
    failures: List[str] = []
    checked = 0
    for row in rows:
        if checked >= sample:
            break
        v = row.get(key)
        if is_missing(v):
            continue
        checked += 1
        non_missing += 1
        if parse_datetime(v) is not None:
            parsed += 1
        elif len(failures) < 5:
            failures.append(str(v))
    if non_missing == 0:
        return False, 0, 0, []
    return (parsed / float(non_missing)) >= threshold, parsed, non_missing, failures


def _F(x: Any) -> Optional[float]:
    """内部：宽容地把 Decimal/int/float/str 变成 float，缺失变 None。"""
    if x is None:
        return None
    if isinstance(x, bool):
        return None
    if isinstance(x, float):
        return x
    if isinstance(x, int):
        return float(x)
    if isinstance(x, Decimal):
        return float(x)
    return to_float(x)


# --------------------------------------------------------------------------
# Result —— 带口径的结论容器
# --------------------------------------------------------------------------

class Result(object):
    """
    一个可复算、可追溯的计算结果。

    属性：
        value        数值（不可计算时为 None）
        numerator    分子（金额或计数），可为 None
        denominator  分母，可为 None
        unit         '元' / '张' / '笔' / '' 等
        formula      所用公式的字符串，写在报告里
        scope        口径说明（对象、时间、状态、去重、归因范围）
        reason       不可计算的原因；可计算时为 ''
        note         需要一起发布的限制提示
        meta         其他结构化细节
    """

    __slots__ = ("value", "numerator", "denominator", "unit", "formula",
                 "scope", "reason", "note", "meta")

    def __init__(self, value: Optional[Any] = None,
                 numerator: Optional[Any] = None,
                 denominator: Optional[Any] = None,
                 unit: str = "",
                 formula: str = "",
                 scope: str = "",
                 reason: str = "",
                 note: str = "",
                 meta: Optional[Dict[str, Any]] = None) -> None:
        self.value = value
        self.numerator = numerator
        self.denominator = denominator
        self.unit = unit
        self.formula = formula
        self.scope = scope
        self.reason = reason
        self.note = note
        self.meta = meta or {}

    @property
    def ok(self) -> bool:
        """False 表示"不可计算"——报告里必须显示"不可计算"，不能显示 0。"""
        return self.value is not None

    def display(self, digits: int = 2, as_percent: bool = False,
                with_denominator: bool = True) -> str:
        """
        渲染成报告用的字符串。不可计算时输出"不可计算（原因）"。
        比例类请传 as_percent=True，例如 0.5 → "50.00%"。
        """
        if not self.ok:
            return "不可计算（%s）" % (self.reason or "分母为 0 或数据不足")
        v = _F(self.value)
        assert v is not None
        if as_percent:
            body = ("%." + str(digits) + "f%%") % (v * 100.0)
        else:
            body = ("%." + str(digits) + "f") % v
            if self.unit:
                body = body + " " + self.unit
        if with_denominator and self.denominator is not None:
            body = "%s（分子 %s / 分母 %s）" % (body, self.numerator, self.denominator)
        return body

    def to_dict(self) -> Dict[str, Any]:
        return {
            "value": None if self.value is None else str(self.value),
            "numerator": None if self.numerator is None else str(self.numerator),
            "denominator": None if self.denominator is None else str(self.denominator),
            "unit": self.unit,
            "formula": self.formula,
            "scope": self.scope,
            "computable": self.ok,
            "reason": self.reason,
            "note": self.note,
            "meta": self.meta,
        }

    def line(self, label: str = "", digits: int = 2,
             as_percent: bool = False) -> str:
        """一行式报告：`标签 = 数值｜公式｜口径`。"""
        head = (label + " = ") if label else ""
        return "%s%s｜公式：%s%s" % (
            head, self.display(digits, as_percent),
            self.formula or "—",
            ("｜口径：" + self.scope) if self.scope else "",
        )

    def __repr__(self) -> str:
        return "Result(%r, num=%r, den=%r, ok=%s)" % (
            self.value, self.numerator, self.denominator, self.ok)


def _nc(reason: str, formula: str = "", scope: str = "",
        numerator: Any = None, denominator: Any = None,
        unit: str = "", note: str = "") -> Result:
    """构造"不可计算"结果。"""
    return Result(value=None, numerator=numerator, denominator=denominator,
                  unit=unit, formula=formula, scope=scope, reason=reason,
                  note=note)


# --------------------------------------------------------------------------
# 一、比例与加权汇总（规范第七节）
# --------------------------------------------------------------------------

def safe_div(numerator: Any, denominator: Any,
             formula: str = "", scope: str = "",
             unit: str = "", note: str = "") -> Result:
    """
    安全的除法：分母为 0 或缺失时返回"不可计算"，**不返回 0**。

    >>> safe_div(0, 0).ok
    False
    >>> safe_div(1, 2).value
    Decimal('0.5')
    """
    n = to_decimal(numerator)
    d = to_decimal(denominator)
    if n is None:
        return _nc("分子缺失，无法计算", formula, scope, numerator=None,
                   denominator=d, unit=unit, note=note)
    if d is None:
        return _nc("分母缺失，无法计算", formula, scope, numerator=n,
                   denominator=None, unit=unit, note=note)
    if d == 0:
        return _nc("分母为 0，不可计算（不得显示为 0%）", formula, scope,
                   numerator=n, denominator=d, unit=unit, note=note)
    return Result(value=(n / d), numerator=n, denominator=d, unit=unit,
                  formula=formula, scope=scope, note=note)


def pooled_ratio(parts: Sequence[Tuple[Any, Any]],
                 formula: str = "Σ分子 / Σ分母",
                 scope: str = "",
                 unit: str = "") -> Result:
    """
    总体比例 = 各组分���合计 / 各组分母合计。**绝不平均各组百分比**。

    parts: [(分子, 分母), ...]

    >>> pooled_ratio([(1, 100), (0, 100)]).value  # 不是 50%
    Decimal('0.005')
    """
    if not parts:
        return _nc("没有任何分组，无法汇总", formula, scope, unit=unit)
    sn = Decimal(0)
    sd = Decimal(0)
    bad: List[int] = []
    for i, (a, b) in enumerate(parts):
        x = to_decimal(a)
        y = to_decimal(b)
        if x is None or y is None:
            bad.append(i)
            continue
        sn += x
        sd += y
    note = ""
    if bad:
        note = ("有 %d 个分组分子或分母缺失，已排除（排除明细见 meta）"
                % len(bad))
    if sd == 0:
        return _nc("各组分母合计为 0，不可计算", formula, scope,
                   numerator=sn, denominator=sd, unit=unit, note=note)
    r = Result(value=(sn / sd), numerator=sn, denominator=sd, unit=unit,
               formula=formula, scope=scope, note=note)
    r.meta["groups"] = len(parts)
    r.meta["excluded_groups"] = bad
    return r


def ratio_of_sums(numerators: Iterable[Any], denominators: Iterable[Any],
                  **kw: Any) -> Result:
    """pooled_ratio 的列表版便捷入口。"""
    return pooled_ratio(list(zip(list(numerators), list(denominators))), **kw)


def pooled_roi(groups: Sequence[Dict[str, Any]],
               formula: str = "总体ROI = Σ各组成交金额 / Σ各组消耗",
               scope: str = "平台归因成交金额／对应广告消耗（须说明归因模型与时间窗口）",
               ) -> Result:
    """
    总体 ROI = 各组对应成交金额合计 / 各组对应消耗合计。

    groups 支持两种写法：
        [{"spend": 100, "gmv": 300}, ...]              # 已知成交金额
        [{"spend": 100, "roi": 3}, ...]                # 已知各组 ROI，先还原成交额
    自检案例 1：消耗 100/ROI 3 与 消耗 10000/ROI 1 → 10300/10100 ≈ 1.0198。

    >>> str(pooled_roi([{"spend":100,"roi":3},{"spend":10000,"roi":1}]).value.quantize(Decimal("0.0001")))
    '1.0198'
    """
    if not groups:
        return _nc("没有任何分组，无法汇总", formula, scope)
    spend_sum = Decimal(0)
    gmv_sum = Decimal(0)
    skipped: List[int] = []
    for i, g in enumerate(groups):
        s = to_decimal(g.get("spend", g.get("消耗")))
        if s is None:
            skipped.append(i)
            continue
        gmv = g.get("gmv", g.get("成交金额"))
        if gmv is None and g.get("roi", g.get("ROI")) is not None:
            r = to_decimal(g.get("roi", g.get("ROI")))
            if r is None:
                skipped.append(i)
                continue
            gmv = s * r
        g = to_decimal(gmv) if not isinstance(gmv, Decimal) else gmv
        if g is None:
            skipped.append(i)
            continue
        spend_sum += s
        gmv_sum += g
    note = ""
    if skipped:
        note = "有 %d 个分组缺少消耗或成交金额，已排除（不得静默重新加权）" % len(skipped)
    if spend_sum == 0:
        return _nc("各分组消耗合计为 0，不可计算", formula, scope,
                   numerator=gmv_sum, denominator=spend_sum, unit="", note=note)
    r = Result(value=(gmv_sum / spend_sum), numerator=gmv_sum,
               denominator=spend_sum, unit="", formula=formula, scope=scope,
               note=note)
    r.meta["skipped_groups"] = skipped
    r.meta["total_spend"] = str(spend_sum)
    r.meta["total_gmv"] = str(gmv_sum)
    return r


def attribution_roi(attributed_gmv: Any, ad_spend: Any,
                    model: str = "",
                    window: str = "") -> Result:
    """
    平台归因 ROI = 平台归因成交金额 / 对应广告消耗。

    必须说明归因模型、时间窗口和成交金额定义。**不等于广告带来的新增成交。**
    """
    scope = "归因模型：%s；时间窗口：%s；成交金额定义见数据契约" % (
        model or "未说明", window or "未说明")
    r = safe_div(attributed_gmv, ad_spend,
                 formula="平台归因ROI = 平台归因成交金额 / 对应广告消耗",
                 scope=scope)
    r.note = ("平台归因成交 ≠ 广告新增成交；若要主张因果，需要可信实验或"
              "有效的对照观察设计（规范第十二节）")
    return r


def refund_order_ratio(refunded_orders: Any, paid_orders_same_cohort: Any) -> Result:
    """退款订单比例 = 有退款的去重订单数 / 同一购买批次的支付订单数。"""
    r = safe_div(refunded_orders, paid_orders_same_cohort,
                 formula="退款订单比例 = 有退款的去重订单数 / 同一购买批次支付订单数",
                 scope="分母为原支付批次；已退款订单默认不从分母剔除")
    r.note = ("若改为分析未退款订单，必须另列指标并明确说明；"
              "不得通过剔除失败订单让比例变好看")
    return r


def redeem_coupon_ratio(redeemed_coupons: Any, purchased_coupons: Any) -> Result:
    """核销券比例 = 有效核销的去重券数 / 同批次购买券数。"""
    return safe_div(redeemed_coupons, purchased_coupons,
                    formula="核销券比例 = 有效核销的去重券数 / 同批次购买券数",
                    scope="券按券码去重；一单多券时券数 ≠ 订单数")


def redeem_order_ratio(redeemed_orders: Any, paid_orders: Any) -> Result:
    """核销订单比例 = 至少有一次有效核销的去重订单数 / 同批次支付订单数。"""
    return safe_div(redeemed_orders, paid_orders,
                    formula="核销订单比例 = 至少一次有效核销的去重订单数 / 同批次支付订单数",
                    scope="订单按订单ID去重；核销行数不是订单数")


# --------------------------------------------------------------------------
# 二、变化量（规范第八节末尾）
# --------------------------------------------------------------------------

def delta_amount(current: Any, base: Any) -> Result:
    """变化金额 = 本期 － 基期。"""
    c = to_decimal(current)
    b = to_decimal(base)
    if c is None or b is None:
        return _nc("本期或基期缺失，无法计算变化金额",
                   "变化金额 = 本期－基期")
    return Result(value=(c - b), numerator=c, denominator=b, unit="",
                  formula="变化金额 = 本期－基期", scope="")


def relative_change(current: Any, base: Any) -> Result:
    """
    相对变化 = (本期－基期)/基期。**基期为 0 时不输出普通增长率。**

    >>> str(relative_change(5, 4).value)
    '0.25'
    """
    c = to_decimal(current)
    b = to_decimal(base)
    if c is None or b is None:
        return _nc("本期或基期缺失", "相对变化 = (本期－基期)/基期")
    if b == 0:
        return _nc("基期为 0，不输出普通增长率（应改用绝对变化或其他口径）",
                   "相对变化 = (本期－基期)/基期", numerator=c, denominator=b)
    return Result(value=((c - b) / b), numerator=c, denominator=b, unit="",
                  formula="相对变化 = (本期－基期)/基期", scope="")


def pct_point_delta(current_ratio: Any, base_ratio: Any) -> Result:
    """
    百分点变化 = 本期比例 － 基期比例。
    CTR 从 4% 到 5%：是提高 **1 个百分点**，相对提升 25%，不是"提升 1%"。
    """
    c = to_float(current_ratio)
    b = to_float(base_ratio)
    if c is None or b is None:
        return _nc("本期或基期比例缺失", "百分点变化 = 本期比例－基期比例")
    # 用 Decimal 相减，避免 0.05-0.04 = 0.010000000000000002 这类浮点噪声
    cd = Decimal(repr(c))
    bd = Decimal(repr(b))
    r = Result(value=(cd - bd), numerator=cd, denominator=bd, unit="个百分点",
               formula="百分点变化 = 本期比例－基期比例",
               scope="比例须为同口径、同分母定义")
    r.note = "百分点变化与相对变化是两个概念，报告里不得混称为『提升X%』"
    return r


# --------------------------------------------------------------------------
# 三、分布描述（规范第八节）
# --------------------------------------------------------------------------

def mean(values: Iterable[Any]) -> Result:
    """均值 = 数值合计 / 记录数量（缺失值不参与，且要报告缺失数量）。"""
    vals: List[Decimal] = []
    miss = 0
    for v in values:
        d = to_decimal(v)
        if d is None:
            miss += 1
        else:
            vals.append(d)
    if not vals:
        return _nc("没有可用数值（全部缺失）", "均值 = 数值合计 / 记录数量",
                   note="缺失 %d 条" % miss)
    total = sum(vals, Decimal(0))
    n = Decimal(len(vals))
    r = Result(value=(total / n), numerator=total, denominator=n, unit="",
               formula="均值 = 数值合计 / 记录数量",
               scope="有效记录 %d 条" % len(vals))
    if miss:
        r.note = "有 %d 条缺失，已排除（缺失不等于 0）" % miss
    r.meta["valid_n"] = len(vals)
    r.meta["missing_n"] = miss
    return r


def quantile_linear(values: Sequence[Any], q: float,
                    assume_sorted: bool = False) -> Result:
    """
    线性插值分位数（与 numpy 默认 method='linear' 一致）。
    **分位数方法必须在全篇保持一致**，不能混用不同算法再比较微小差异。

    q = 0.5 → 中位数 P50；0.25 → P25；0.75 → P75；0.9 → P90
    """
    if not 0.0 <= q <= 1.0:
        return _nc("分位点必须落在 [0,1]", "线性插值分位数")
    vals: List[float] = []
    miss = 0
    for v in values:
        f = _F(v)
        if f is None:
            miss += 1
        else:
            vals.append(f)
    if not vals:
        return _nc("没有可用数值", "线性插值分位数")
    if not assume_sorted:
        vals = sorted(vals)
    n = len(vals)
    if n == 1:
        return Result(value=Decimal(repr(vals[0])), numerator=None,
                      denominator=Decimal(n), unit="",
                      formula="线性插值分位数 P%s" % (q * 100),
                      scope="有效记录 %d 条" % n,
                      note=("缺失 %d 条已排除" % miss) if miss else "")
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        val = vals[lo]
    else:
        frac = pos - lo
        val = vals[lo] + (vals[hi] - vals[lo]) * frac
    r = Result(value=Decimal(repr(val)), denominator=Decimal(n), unit="",
               formula="线性插值分位数 P%s" % (q * 100),
               scope="有效记录 %d 条" % n)
    if miss:
        r.note = "有 %d 条缺失，已排除（缺失不等于 0）" % miss
    return r


def median(values: Sequence[Any]) -> Result:
    """中位数 = P50（线性插值）。"""
    r = quantile_linear(values, 0.5)
    r.formula = "中位数 = 线性插值 P50"
    return r


def percentile_report(values: Sequence[Any],
                      qs: Sequence[float] = (0.0, 0.25, 0.5, 0.75, 0.9, 1.0),
                      ) -> Dict[str, Result]:
    """
    分布描述：最小值、P25、P50、P75、P90、最大值、均值、标准差、变异系数。
    样本只有 9 天时，只能报告这 9 天的范围与标准差，
    **不能称其为稳定的正常区间**（规范第八节）。
    """
    vals = [v for v in (_F(x) for x in values) if v is not None]
    out: Dict[str, Result] = {}
    for q in qs:
        label = ("min" if q == 0.0 else "max" if q == 1.0
                 else "P%d" % int(round(q * 100)))
        out[label] = quantile_linear(values, q)
    out["mean"] = mean(values)
    out["stdev"] = sample_stdev(values)
    out["cv"] = cv(values)
    out["count"] = Result(value=Decimal(len(vals)), denominator=Decimal(len(vals)),
                          unit="条", formula="有效记录数", scope="缺失值已排除")
    return out


def sample_stdev(values: Sequence[Any]) -> Result:
    """
    样本标准差 = sqrt[Σ(x－均值)² / (n－1)]。**n < 2 时不可计算。**
    """
    vals = [v for v in (_F(x) for x in values) if v is not None]
    n = len(vals)
    if n < 2:
        return _nc("n = %d < 2，样本标准差不可计算" % n,
                   "样本标准差 = sqrt[Σ(x－均值)² / (n－1)]",
                   denominator=Decimal(n))
    m = sum(vals) / n
    ss = sum((x - m) ** 2 for x in vals)
    var = ss / (n - 1)
    r = Result(value=Decimal(repr(math.sqrt(var))), denominator=Decimal(n),
               unit="", formula="样本标准差 = sqrt[Σ(x－均值)² / (n－1)]",
               scope="有效记录 %d 条（样本标准差，非总体标准差）" % n)
    r.meta["mean"] = m
    r.meta["variance"] = var
    return r


def cv(values: Sequence[Any]) -> Result:
    """
    变异系数 = 标准差 / 均值。**仅在均值为正且有解释意义时使用。**
    """
    st = sample_stdev(values)
    if not st.ok:
        return _nc("样本标准差不可计算（%s）" % st.reason,
                   "变异系数 = 标准差 / 均值")
    m = _F(mean(values).value)
    sd = _F(st.value)
    if m is None or sd is None:
        return _nc("均值或标准差缺失", "变异系数 = 标准差 / 均值")
    if m <= 0:
        return _nc("均值 ≤ 0，变异系数没有解释意义",
                   "变异系数 = 标准差 / 均值", numerator=Decimal(repr(sd)),
                   denominator=Decimal(repr(m)))
    r = safe_div(Decimal(repr(sd)), Decimal(repr(m)),
                 formula="变异系数 = 标准差 / 均值",
                 scope="仅适用于均值为正且量纲可比的场景")
    r.note = "均值接近 0 时变异系数会爆炸，不要据此排名判定好或坏"
    return r


def iqr_outliers(values: Sequence[Any], k: float = 1.5) -> Dict[str, Any]:
    """
    IQR 异常候选筛查：IQR = P75 － P25；
    低于 P25 － 1.5×IQR 或高于 P75 ＋ 1.5×IQR 的记录**标记待查**。

    ⚠️ 这只是筛查规则，**不是删除依据**（规范第八节、第四节第 8 条）。
    返回 {"q1","q3","iqr","low","high","outlier_indexes","note"}。
    """
    vals = [v for v in (_F(x) for x in values) if v is not None]
    if len(vals) < 4:
        return {"q1": None, "q3": None, "iqr": None, "low": None, "high": None,
                "outlier_indexes": [],
                "note": "有效记录少于 4 条，IQR 筛查不可靠，不输出界限"}
    ordered = sorted(vals)
    n = len(ordered)

    def _q(qq: float) -> float:
        pos = qq * (n - 1)
        lo, hi = int(math.floor(pos)), int(math.ceil(pos))
        if lo == hi:
            return ordered[lo]
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)

    q1, q3 = _q(0.25), _q(0.75)
    iqr = q3 - q1
    low = q1 - k * iqr
    high = q3 + k * iqr
    idx = [i for i, x in enumerate(vals) if x < low or x > high]
    return {
        "q1": q1, "q3": q3, "iqr": iqr, "low": low, "high": high,
        "outlier_indexes": idx,
        "note": ("共标记 %d 条待查；IQR 只是筛查规则，不是删除依据。"
                 "必须先核实是否真实大单、多份购买、重复记录或数据错误" % len(idx)),
    }


def head_concentration(values: Sequence[Any], top: int = 5) -> Dict[str, Any]:
    """
    头部集中度：前 N 名占总量的比例。用于判断"总量被少数几个对象主导"。
    """
    vals = [v for v in (_F(x) for x in values) if v is not None]
    if not vals:
        return {"top_n": top, "share": None, "note": "没有可用数值"}
    total = sum(vals)
    if total == 0:
        return {"top_n": top, "share": None,
                "note": "合计为 0，集中度不可计算"}
    top_vals = sorted(vals, reverse=True)[:top]
    return {"top_n": top, "top_sum": sum(top_vals), "total": total,
            "share": sum(top_vals) / total,
            "note": "头部集中度高时，总量的波动主要由少数对象驱动"}


# --------------------------------------------------------------------------
# 四、样本量与统计不确定性（规范第十一节）
# --------------------------------------------------------------------------

def wilson_interval(k: Any, n: Any, z: float = 1.96) -> Result:
    """
    Wilson 比例区间（通常对应 95% 区间，z = 1.96）：

        p = k / n
        中心   = [p + z²/(2n)] / [1 + z²/n]
        半宽   = z × sqrt[p(1－p)/n + z²/(4n²)] / [1 + z²/n]
        区间   = 中心 ± 半宽

    限制（必须一起发布）：
      - k 必须是明确的成功次数，n 是对应试验次数；n 必须 > 0；
      - 一人多次曝光、同群流量高度相关时，**不能默认每次曝光都是独立样本**；
      - **不要用于 ROI 或客单价**；
      - 不要用两个区间是否重叠代替正式的差异比较。
    """
    kk = to_float(k)
    nn = to_float(n)
    if kk is None or nn is None:
        return _nc("k 或 n 缺失", "Wilson 比例区间")
    if nn <= 0:
        return _nc("n 必须大于 0", "Wilson 比例区间", denominator=Decimal(repr(nn)))
    if kk < 0 or kk > nn:
        return _nc("k 必须满足 0 ≤ k ≤ n", "Wilson 比例区间",
                   numerator=Decimal(repr(kk)), denominator=Decimal(repr(nn)))
    p = kk / nn
    z2 = z * z
    denom = 1.0 + z2 / nn
    center = (p + z2 / (2.0 * nn)) / denom
    half = (z * math.sqrt(p * (1.0 - p) / nn + z2 / (4.0 * nn * nn))) / denom
    r = Result(value=Decimal(repr(p)), numerator=Decimal(repr(kk)),
               denominator=Decimal(repr(nn)), unit="",
               formula=("Wilson 区间：中心 = [p + z²/(2n)]/[1 + z²/n]；"
                        "半宽 = z×sqrt[p(1－p)/n + z²/(4n²)]/[1 + z²/n]"),
               scope="独立二项试验假设；z = %.2f（通常对应 95%% 区间）" % z)
    r.meta.update({
        "p": p, "z": z, "center": center, "half_width": half,
        "lower": max(0.0, center - half), "upper": min(1.0, center + half),
    })
    r.note = ("置信区间只表达抽样不确定性，不能弥补错误归因、数据缺失或系统性偏差；"
              "没有显著差异不等于两者相同；p 值不是原假设为真的概率；"
              "统计显著不等于改善足够大，也不等于赚钱")
    return r


# --------------------------------------------------------------------------
# 五、同批次、同观察年龄的核销与退款（规范第九节）
# --------------------------------------------------------------------------

def mature_cohort(rows: Sequence[Dict[str, Any]],
                  t0_key: str,
                  cutoff: Any,
                  hours: float,
                  parse_time=None) -> Dict[str, Any]:
    """
    成熟条件：某笔订单可进入 H 窗口分析 ⇔ **t0 + H <= T**。

    t0    = 每笔订单的支付时间（t0_key 指定字段）
    T     = 可靠的数据观察截止时间（cutoff）
    H     = 指定观察时长（hours，单位小时）

    返回：
      eligible_indexes / immature_indexes / unparsable_indexes
      eligible_rows / immature_rows
      hours / cutoff / t0_key
      note  —— 提醒"不足 H 的订单单独报告，不进入固定窗口比较"
    """
    from datetime import datetime, timedelta

    if parse_time is None:
        parse_time = parse_datetime

    T = parse_time(cutoff) if not isinstance(cutoff, datetime) else cutoff
    if T is None:
        raise ValueError("观察截止时间 T 无法解析：%r（必须明确给出，否则不能做成熟批次分析）"
                         % (cutoff,))

    eligible: List[int] = []
    immature: List[int] = []
    unparsable: List[int] = []
    for i, row in enumerate(rows):
        t0 = parse_time(row.get(t0_key))
        if t0 is None:
            unparsable.append(i)
            continue
        if t0 + timedelta(hours=hours) <= T:
            eligible.append(i)
        else:
            immature.append(i)
    return {
        "t0_key": t0_key, "cutoff": T, "hours": hours,
        "eligible_indexes": eligible, "immature_indexes": immature,
        "unparsable_indexes": unparsable,
        "eligible_rows": [rows[i] for i in eligible],
        "immature_rows": [rows[i] for i in immature],
        "n_eligible": len(eligible), "n_immature": len(immature),
        "n_unparsable": len(unparsable),
        "note": ("不足 %.0f 小时的订单共 %d 笔，单独报告，不进入固定窗口比较；"
                 "不能把今天核销额除以今天成交额当作真实核销率" % (hours, len(immature))),
    }


def cohort_rate(rows: Sequence[Dict[str, Any]],
                t0_key: str,
                event_key: str,
                cutoff: Any,
                hours: float,
                event_time_key: Optional[str] = None,
                dedupe_key: Optional[str] = None,
                label: str = "H窗口核销订单比例",
                parse_time=None,
                ) -> Result:
    """
    H 窗口核销（或退款）比例：

        分子 = 满足成熟条件、且支付后 H 内发生有效事件的去重对象数
        分母 = 满足成熟条件的对象数

    正确用法（自检案例 3）：截止今天 3 笔订单
        A 支付 5 天且 24h 内核销、B 支付 4 天未核销、C 只支付 3 小时未核销
        → 只有 A、B 满 24h 窗口 → 比例 = 1/2 = 50%，**不是 1/3**。

    event_key：事件标志列（有值/为真表示"该对象在此表中有事件记录"）。
      若 event_time_key 提供，则要求事件时间落在 (t0, t0+H] 内才算命中。
    dedupe_key：去重键（订单ID／券码）。核销行可能一单多行，必须先按对象去重。
    """
    from datetime import datetime, timedelta

    _p = parse_time if parse_time is not None else parse_datetime

    T = cutoff if isinstance(cutoff, datetime) else _p(cutoff)
    if T is None:
        return _nc("观察截止时间 T 无法解析，不能做成熟批次分析",
                   "H窗口比例 = 成熟且H内发生事件的去重对象数 / 成熟对象数")

    cohorts = mature_cohort(rows, t0_key, T, hours, parse_time=_p)
    eligible = cohorts["eligible_rows"]

    # 一单多券时同一对象会有多行。命中判定必须看**该对象的任意一行**，
    # 不能只看第一行（第一行可能恰好是没有核销时间的那张券）。
    by_object: Dict[Any, List[Dict[str, Any]]] = {}
    for row in eligible:
        obj = row.get(dedupe_key) if dedupe_key else id(row)
        obj = obj if obj is not None else id(row)
        by_object.setdefault(obj, []).append(row)

    hit = 0
    for obj, obj_rows in by_object.items():
        ok = False
        for row in obj_rows:
            t0 = _p(row.get(t0_key))
            ev = row.get(event_key)
            if event_time_key is not None:
                te = _p(row.get(event_time_key))
                if t0 is not None and te is not None and t0 < te <= t0 + timedelta(hours=hours):
                    ok = True
                    break
            else:
                yes = not is_missing(ev)
                if isinstance(ev, str):
                    yes = yes and ev.strip() not in ("0", "否", "未核销", "未退款",
                                                     "false", "False", "无")
                if isinstance(ev, (int, float)) and not isinstance(ev, bool):
                    yes = yes and ev != 0
                if yes:
                    ok = True
                    break
        if ok:
            hit += 1

    n_eligible = len(by_object)

    # ---- 安全网：事件时间列解析全失败时，绝不能输出 0% ----
    # 最危险的场景是把「订单状态」这类类别列误当成时间列：分子恒为 0，
    # 报告却显示一个漂亮的 0.00%，看起来像"核销率极低"，实际是口径错误。
    if event_time_key is not None and n_eligible > 0:
        ev_parsed = 0
        ev_non_missing = 0
        ev_fail_samples: List[str] = []
        for row in eligible:
            raw = row.get(event_time_key)
            if is_missing(raw):
                continue
            ev_non_missing += 1
            if _p(raw) is not None:
                ev_parsed += 1
            elif len(ev_fail_samples) < 5:
                ev_fail_samples.append(str(raw))
        if ev_non_missing > 0 and ev_parsed == 0:
            return _nc(
                "事件列 `%s` 有 %d 个非空取值，但**没有一个能按时间格式解析**"
                "（样例：%s）。它很可能是状态/类别列，而不是时间列。"
                "请改用真正的事件时间列，或去掉 --event-time 以改用『有值即命中』的语义。"
                % (event_time_key, ev_non_missing, "、".join(ev_fail_samples)),
                formula="H窗口比例 = 满足成熟条件且支付后H内发生事件的去重对象数 / 满足成熟条件的对象数",
                scope="H = %g 小时；T = %s" % (hours, T),
                numerator=0, denominator=n_eligible,
                note="拒绝输出 0%：0% 与『口径用错』是两回事，不能混淆")

    r = safe_div(hit, n_eligible,
                 formula="H窗口%s = 满足成熟条件且支付后H内发生事件的去重对象数 / 满足成熟条件的对象数"
                         % label.replace("H窗口", "").replace("比例", ""),
                 scope="H = %g 小时；T = %s；去重键 = %s；仅统计 t0 + H ≤ T 的对象"
                       % (hours, T, dedupe_key or "（未指定，按行计）"))
    r.note = (cohorts["note"] +
              "；未核销、未退款的成熟对象仍保留在原始分母中；"
              "不得把截至目前未核销当作永远不会核销")
    r.meta["n_hit"] = hit
    r.meta["n_eligible"] = n_eligible
    r.meta["n_immature"] = cohorts["n_immature"]
    r.meta["n_unparsable"] = cohorts["n_unparsable"]
    return r


def cohort_curve(rows: Sequence[Dict[str, Any]],
                 t0_key: str,
                 event_time_key: str,
                 cutoff: Any,
                 hours_list: Sequence[float] = (1, 6, 24, 72, 168),
                 dedupe_key: Optional[str] = None,
                 parse_time=None) -> List[Result]:
    """
    核销随时间的增长：**对同一成熟批次计算多个观察节点**。

    禁止：把不同购买批次的 24h 率与 72h 率相减，
          声称得到了"第 2—3 天新增核销率"。
    本函数对同一批成熟订单（成熟条件按最大的 H 判定）计算各节点比例，
    因此各节点之间的差确实代表该批次内的增量。

    注意：只有当**最大 H 也满足成熟条件**的对象集合，才允许跨节点相减。
    """
    from datetime import datetime, timedelta

    def _p(v: Any) -> Optional[datetime]:
        if parse_time is not None:
            return parse_time(v)
        if isinstance(v, datetime):
            return v
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.lower() in MISSING_TOKENS:
            return None
        for f in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M",
                  "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                return datetime.strptime(s, f)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(s.replace("Z", ""))
        except Exception:
            return None

    T = cutoff if isinstance(cutoff, datetime) else _p(cutoff)
    if T is None or not hours_list:
        return []
    hmax = max(hours_list)
    cohorts = mature_cohort(rows, t0_key, T, hmax, parse_time=_p)
    eligible = cohorts["eligible_rows"]

    # 去重：同一对象的多次核销只保留最早一次事件时间
    first_event: Dict[Any, Any] = {}
    for row in eligible:
        obj = row.get(dedupe_key) if dedupe_key else id(row)
        obj = obj if obj is not None else id(row)
        t0 = _p(row.get(t0_key))
        te = _p(row.get(event_time_key))
        if t0 is None or te is None:
            continue
        if obj not in first_event or te < first_event[obj][1]:
            first_event[obj] = (t0, te)

    n = len({(r.get(dedupe_key) if dedupe_key else id(r)) for r in eligible})
    out: List[Result] = []
    for h in sorted(hours_list):
        hit = 0
        for obj, (t0, te) in first_event.items():
            if t0 < te <= t0 + timedelta(hours=h):
                hit += 1
        r = safe_div(hit, n,
                     formula="该成熟批次在 %g 小时内的核销率 = H内已核销去重对象数 / 批次内去重对象数" % h,
                     scope="同一成熟批次（t0 + %g 小时 ≤ %s），各节点可相减" % (hmax, T))
        r.meta["hours"] = h
        r.meta["n_hit"] = hit
        r.meta["n_cohort"] = n
        r.note = ("跨节点相减得到的是**同一批次**的增量；"
                  "不得用不同批次相减冒充增量")
        out.append(r)
    return out


# --------------------------------------------------------------------------
# 六、去重与标准化（规范第五、十节）
# --------------------------------------------------------------------------

def dedupe_count(rows: Sequence[Dict[str, Any]], key: str) -> Dict[str, Any]:
    """
    按对象去重计数。一单多券时：订单数按订单ID去重、券数按券码去重。

    特别提醒：核销行里的"购买数量"可能是原订单购买总数，
    **不能把每行这个字段相加当作核销券数**。
    """
    seen = set()
    missing = 0
    for row in rows:
        v = row.get(key)
        if is_missing(v):
            missing += 1
            continue
        seen.add(str(v).strip())
    return {
        "key": key,
        "rows": len(rows),
        "distinct": len(seen),
        "rows_per_object": (len(rows) - missing) / len(seen) if seen else None,
        "missing_key_rows": missing,
        "note": ("行数 %d ≠ 对象数 %d；跨表合并前必须先按对象聚合"
                 % (len(rows), len(seen))),
    }


def standardize_ratio(groups: Sequence[Dict[str, Any]],
                      weights: Sequence[float]) -> Result:
    """
    标准化比率 = Σ(w × 各组比率)，w 必须公开说明、总和为 1，
    各组必须有可用分母；**不能默默删除缺失组再重新加权**。

    groups: [{"name": "门店A", "numerator": 10, "denominator": 100}, ...]
    """
    if len(groups) != len(weights):
        return _nc("权重个数与分组个数不一致，拒绝计算",
                   "标准化比率 = Σ(w × 各组比率)")
    wsum = sum(weights)
    if abs(wsum - 1.0) > 1e-9:
        return _nc("权重之和 = %.6f ≠ 1，拒绝计算（不得默默重新加权）" % wsum,
                   "标准化比率 = Σ(w × 各组比率)")
    acc = Decimal(0)
    detail: List[Dict[str, Any]] = []
    missing_groups: List[str] = []
    for g, w in zip(groups, weights):
        r = safe_div(g.get("numerator"), g.get("denominator"),
                     formula="各组比率 = 各组分子/各组分母")
        name = str(g.get("name", ""))
        if not r.ok:
            missing_groups.append(name)
            detail.append({"name": name, "weight": w, "ratio": None,
                           "reason": r.reason})
            continue
        acc += Decimal(repr(w)) * r.value
        detail.append({"name": name, "weight": w, "ratio": float(r.value)})
    res = Result(value=acc, unit="",
                 formula="标准化比率 = Σ(w × 各组比率)",
                 scope="共同权重 w 已公开；权重之和为 1")
    if missing_groups:
        res.note = ("以下分组缺少可用分母，已被排除：%s。"
                    "不得默默删除缺失组再重新加权，须在结论中明示" %
                    "、".join(missing_groups))
    res.meta["detail"] = detail
    res.meta["missing_groups"] = missing_groups
    return res


# --------------------------------------------------------------------------
# 七、护栏函数：证据分级、因果防线、金额口径防线、样本单位（规范第七、十一、十二节）
# --------------------------------------------------------------------------

EVIDENCE_LABELS = ("已对账事实", "观察性差异", "待验证假设", "数据不足")
"""默认不要给主观『可信度87%』或任意机会分。只用这四个标签。"""

_KIND_TO_LABEL = {
    "reconciled": "已对账事实", "fact": "已对账事实", "已对账事实": "已对账事实",
    "observational": "观察性差异", "diff": "观察性差异", "观察性差异": "观察性差异",
    "hypothesis": "待验证假设", "假设": "待验证假设", "待验证假设": "待验证假设",
    "insufficient": "数据不足", "数据不足": "数据不足",
}


def evidence_label(kind: str) -> Result:
    """
    把结论类型映射到规范规定的四个证据标签之一。
    禁止使用主观"可信度 87%"或任意机会分。
    """
    lab = _KIND_TO_LABEL.get(str(kind).strip())
    if lab is None:
        return _nc("未知的证据类型：%r；只允许 %s" % (kind, "／".join(EVIDENCE_LABELS)),
                   "证据标签 = 已对账事实／观察性差异／待验证假设／数据不足")
    return Result(value=lab, formula="证据标签分级（规范第十四节）",
                  scope="标签必须与结论的支撑强度一致")


def causal_increment_guard(has_attribution: bool = False,
                           has_control_or_preperiod: bool = False,
                           has_random_assignment: bool = False,
                           note: str = "") -> Dict[str, Any]:
    """
    广告／投放的"新增成交"因果防线（规范第十二节）。

    "平台归因成交"不等于"广告新增成交"。
    即使消费 400 元、平台归因成交 4 万元，
    也不能自动断言"不投广告就会少卖 4 万元"。

    只有具备随机分配（A/B）时才能宣称因果；
    仅前后对比或控制城市/商品/预算 → 标记为**对照观察**，须写明人群、时间、平台分流限制。
    """
    if has_random_assignment and has_control_or_preperiod:
        verdict, label = "可主张因果（须附实验设计与停止规则）", "已对账事实"
        detail = "存在随机分配与对照；仍需报告样本、观察窗口、主指标、风险指标与停止规则"
    elif has_control_or_preperiod:
        verdict, label = "只能说时间关联／对照观察，不能称因果", "观察性差异"
        detail = ("控制城市、商品和预算不等于完成随机实验；"
                  "必须写明人群选择、时间、平台分流等限制")
    elif has_attribution:
        verdict, label = "只能说平台归因，不能称新增", "待验证假设"
        detail = ("平台归因成交受归因窗口与模型影响；缺少可信的无投放比较结果，"
                  "不能换算成新增成交")
    else:
        verdict, label = "数据不足，不能计算广告新增ROI", "数据不足"
        detail = ("缺少广告归因数据和可信的无投放比较结果；"
                  "不得用『总成交额 ÷ 消耗』冒充广告新增ROI")
    return {"verdict": verdict, "evidence_label": label, "detail": detail,
            "has_attribution": bool(has_attribution),
            "has_control_or_preperiod": bool(has_control_or_preperiod),
            "has_random_assignment": bool(has_random_assignment),
            "note": note}


def net_revenue_guard(has_reconciliation: bool = False,
                      has_fulfillment_cost: bool = False,
                      has_fee_and_subsidy_policy: bool = False,
                      note: str = "") -> Dict[str, Any]:
    """
    金额口径防线（规范第七节）。

    金额必须区分：用户实付、订单实收、预计收入、退款、结算收入、利润。
    未经对账，不得把『用户实付－退款』或『订单实收－退款』自动称为净收入。
    缺少履约成本、服务费、补贴处理等信息时，**不计算利润**。

    例：用户实付 98 元、订单实收 100 元、退款 99 元，
    不能直接把 98－99 = －1 元当成经营亏损。
    """
    can_net_revenue = bool(has_reconciliation and has_fee_and_subsidy_policy)
    can_profit = bool(can_net_revenue and has_fulfillment_cost)
    if can_profit:
        label, verdict = "已对账事实", "可计算利润（须列出成本与费用的完整口径）"
    elif can_net_revenue:
        label, verdict = "观察性差异", "可谈净收入，不可谈利润（缺履约成本）"
    else:
        label, verdict = "数据不足", "不可称为净收入，也不可称为亏损"
    detail = ("须先检查优惠、补贴、退款口径和费用；"
              "相差 1~2 元的实付/实收差异往往来自优惠与补贴，不等于经营亏损")
    return {"verdict": verdict, "evidence_label": label, "detail": detail,
            "can_net_revenue": can_net_revenue, "can_profit": can_profit,
            "note": note}


def independent_units_guard(n_records: Any = None,
                            n_periods: Any = None,
                            n_units: Any = None,
                            unit_kind: str = "",
                            note: str = "") -> Dict[str, Any]:
    """
    样本单位防线（规范第十一节）。

    2,000 笔订单不等于 2,000 次独立投放实验。
    同一用户多次购买、同一门店集中成交、同一计划算法分配流量，都可能形成相关性。
    **禁止固定门槛**：不存在"1000 曝光一定够""30 个样本就可靠""7 天一定可以下结论"。
    """
    rec = to_float(n_records)
    per = to_float(n_periods)
    un = to_float(n_units)
    if un is None:
        un = per
    reasons: List[str] = []
    record_level_n_usable = True
    if rec is not None and un is not None and rec > un:
        reasons.append("记录数(%s) 远大于独立单位数(%s)，存在组内相关"
                       % (int(rec), int(un)))
        record_level_n_usable = False
    if per is not None and per < 3:
        reasons.append("干预单位只有 %s 个，不足以做统计推断" % int(per))
    if un is not None and un < 10:
        reasons.append("独立单位 %s 个，只能作探索性观察" % int(un))
    ok = bool(un is not None and un >= 10)
    return {
        "n_records": rec, "n_periods": per, "n_independent_units": un,
        "unit_kind": unit_kind or "未说明",
        "ok_to_infer": ok,
        "record_level_n_usable": record_level_n_usable,
        "evidence_label": "观察性差异" if ok else "数据不足",
        "reasons": reasons,
        "note": (note or ("不得把记录数当作独立样本数；"
                          "不得使用固定门槛（1000曝光/30样本/7天）；"
                          "样本计划应取决于基准水平、需识别的最小业务差异、"
                          "误判容忍度、统计功效与实验单位")),
    }


def multiple_comparison_guard(n_tests: Any) -> Dict[str, Any]:
    """
    多重比较防线（规范第十一节）：大量商品／计划同时比较时，
    挑出的"第一名"不能宣布已被统计证明最好。
    """
    n = to_float(n_tests)
    if n is None or n < 1:
        return {"n_tests": n, "verdict": "数据不足",
                "evidence_label": "数据不足", "note": "比较对象个数未知"}
    if n >= 10:
        return {"n_tests": int(n),
                "verdict": "结果只能标注为探索性筛查",
                "evidence_label": "待验证假设",
                "note": ("同时比较 %d 个对象，存在多重比较问题；不熟悉适当方法时，"
                         "不得宣布第一名已被统计证明最好；"
                         "建议先做筛选，再对少数候选单独验证") % int(n)}
    return {"n_tests": int(n), "verdict": "比较对象较少，仍须报告区间",
            "evidence_label": "观察性差异",
            "note": "样本量小时区间很宽，不要用微小差异排名"}


# --------------------------------------------------------------------------
# 自检案例（规范第二部分的 8 个案例，可执行版）
# --------------------------------------------------------------------------

def cases_from_spec() -> List[Dict[str, Any]]:
    """
    把规范自检题里的**期望结果**转成可断言的期望值。
    真正的断言在 selftest.py 里执行（那里会调用本库与 reconcile.py）。
    本函数只负责给出题目与期望，供 CLI `metrics.py selftest` 与测试复用。
    """
    return [
        {"id": 1, "title": "总体ROI",
         "expect": "10300/10100 ≈ 1.0198（不是 2）"},
        {"id": 2, "title": "一单多券",
         "expect": "1笔订单、2张核销券、核销金额200元（不是2笔/4张/400元）"},
        {"id": 3, "title": "观察窗口",
         "expect": "24小时核销比例 = 1/2 = 50%（不是 1/3）"},
        {"id": 4, "title": "比例变化",
         "expect": "增加 1 个百分点，相对增加 25%"},
        {"id": 5, "title": "广告归因",
         "expect": "不能算 60000/400 并声称广告新增ROI=150；数据不足"},
        {"id": 6, "title": "未匹配核销",
         "expect": "9月批次分子不含8月购买的30张；门店接待量可含"},
        {"id": 7, "title": "退款口径",
         "expect": "不能直接把 98-99=-1 当作经营亏损；口径不足"},
        {"id": 8, "title": "样本单位",
         "expect": "3000笔订单 ≠ 3000个独立样本；只有2天投放记录"},
    ]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _selftest_cli() -> int:
    print("规范自检题期望值（完整断言请在 selftest.py / tests 里跑）：")
    for c in cases_from_spec():
        print("  案例%d %s → %s" % (c["id"], c["title"], c["expect"]))
    print("")
    print("提示：python3 scripts/selftest.py  会真正执行断言并逐条打印 PASS/FAIL。")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="抖音来客／本地推 统计公式库（可复算、带口径、拒绝用 0 冒充不可计算）")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("selftest", help="打印规范自检题的期望值")
    args = p.parse_args(argv)
    if args.cmd == "selftest":
        return _selftest_cli()
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
