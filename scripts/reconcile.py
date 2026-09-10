# -*- coding: utf-8 -*-
"""抖音来客 / 本地推 经营数据统计审计 —— 对账规则程序化校验模块。

本模块把"最危险的错误"做成可执行的程序校验，而不是靠人盯：

1. **跨表关联导致金额被重复累计**（一单多券：1 笔订单 200 元买 2 张券，
   核销表 2 行各 100 元，直接按订单ID关联后左表金额被放大成 400 元）。
   -> ``join_audit``：关联前/后行数与金额列合计、金额放大倍数、
      未匹配键分类、verdict（通过 / 警告 / 失败-禁止使用该结果）。
   -> ``aggregate_then_join``：内置"按对象聚合后再 1:1 关联"的正确做法。

2. **分组回加对不上总量却照常出报告**。
   -> ``check_additivity``：分组数、各组行数与合计、分组回加 vs 总量的
      差额（差 1 分钱也报），并指出哪些组可能缺失或重复。
   -> ``check_partition``：分组是否互斥（同一行是否可能落入多个分组）。

3. **一单多券时"核销行里的购买数量"是原订单购买总数**，逐行相加会把券数翻倍。
   -> ``dedupe_rows`` / ``aggregate_then_join`` 的聚合方案与理由，明确提示
      "购买数量应取 max/first，绝不能 sum"。

约定与实现要点
--------------
* 只依赖标准库；金额一律 ``decimal.Decimal``，绝不用 float 累加。
* 表对象来自同目录的 ``contract.py``（``Table`` / ``load_table(path)``），
  本模块只读取其 ``name, sheet, headers, rows, source, n_rows, n_cols`` 属性，
  所有单元格按字符串处理（``None`` 会当作空字符串）。
* 缺失值（``-`` / ``""`` / ``"N/A"`` 等）一律视为 ``None``，**绝不是 0**；
  合计时缺失值被跳过并单独计数上报。
* 空键（``""`` 或占位符）**不参与关联**：否则所有空键会互相匹配，
  造成虚假关联。空键行走"未匹配 -> 键缺失(空)"分支。
* 键匹配对空白字符不敏感（Excel 导出的前后空格/全角空格很常见），
  被规范化过的键会在报告中提示。

作者：数据审计技能模块（可直接作为库使用，也可用 CLI 调用）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, OrderedDict
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "CONTRACT_AVAILABLE",
    "CONTRACT_HINT",
    "column_index",
    "to_decimal",
    "key_multiplicity",
    "classify_relationship",
    "join_audit",
    "aggregate_then_join",
    "group_sums",
    "check_additivity",
    "check_partition",
    "dedupe_rows",
    "reconciliation_report",
    "main",
    "VERDICT_PASS",
    "VERDICT_WARN",
    "VERDICT_FAIL",
]

# ---------------------------------------------------------------------------
# 0. contract.py 导入（同事提供：Table / load_table(path)->Table）
# ---------------------------------------------------------------------------

CONTRACT_HINT = (
    "缺少 scripts/contract.py：本模块需要与 reconcile.py 同目录的 contract.py "
    "提供 Table 与 load_table(path)->Table。请让数据层同事补上该文件；"
    "在它出现之前，也可以把它所在目录挂到 PYTHONPATH 上再运行，例如："
    "PYTHONPATH=/path/to/contract_dir python3 scripts/reconcile.py ..."
)


def _load_contract() -> Tuple[Any, Any, Optional[BaseException]]:
    """先按常规导入，再按本文件所在目录兜底导入 contract。"""
    try:
        from contract import Table, load_table  # type: ignore
        return Table, load_table, None
    except ImportError as first_exc:
        here = os.path.dirname(os.path.abspath(__file__))
        if here and here not in sys.path:
            sys.path.insert(0, here)
        try:
            from contract import Table, load_table  # type: ignore
            return Table, load_table, None
        except ImportError as second_exc:
            return None, None, second_exc or first_exc


def _missing_load_table(path: str) -> Any:  # pragma: no cover - 仅在缺依赖时触发
    raise RuntimeError(CONTRACT_HINT)


Table, load_table, _CONTRACT_IMPORT_ERROR = _load_contract()  # noqa: F811
CONTRACT_AVAILABLE = load_table is not None
if not CONTRACT_AVAILABLE:  # 缺依赖时给出清晰中文提示，但不阻断函数级复用
    load_table = _missing_load_table
    print("【环境提示】" + CONTRACT_HINT, file=sys.stderr)

VERDICT_PASS = "通过"
VERDICT_WARN = "警告"
VERDICT_FAIL = "失败-禁止使用该结果"

_ABS_EPS = Decimal("0.01")
_QUANTITY_HINT = "右表『购买数量』若为订单购买总数，应取 max/first，绝不能 sum"

_WS_RE = re.compile(r"[\s\u3000\u00a0]+")
_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_CURRENCY_RE = re.compile(r"[¥￥$€£]|人民币|rmb|cny|RMB|CNY|元|圆|整")
_DATE_LIKE_RE = re.compile(
    r"\d{4}\s*[-/年]\s*\d{1,2}"
    r"|\d{4}\.\d{1,2}\.\d{1,2}"
    r"|\d{1,2}\s*:\s*\d{2}"
)  # 注意：不能用"数字.数字"判日期，否则 1000.00 会被误判成日期
_DATE_COL_NAME_RE = re.compile(r"日期|时间|月份|年月|期间|周期|date|time|month|week")
_QUANTITY_COL_RE = re.compile(
    r"数量|件数|张数|份数|人数|个数|笔数|台数|次数|购买数|券数|单数|qty|quantity",
    re.IGNORECASE,
)
_AMOUNT_COL_RE = re.compile(
    r"金额|额|价格|单价|价|费用|费|收入|营收|实收|应收|应付|退款|补贴|佣金|优惠|"
    r"amount|price|fee|revenue|cost|gmv",
    re.IGNORECASE,
)
_IDENT_COL_RE = re.compile(
    r"券码|券号|订单|单号|编号|编码|号|id|code|手机|电话|用户|顾客|会员|名称|name|sku|商品",
    re.IGNORECASE,
)

_MISSING_TOKENS = {
    "",
    "-",
    "--",
    "---",
    "—",
    "–",
    "－",
    "−",
    "/",
    "\\",
    "n/a",
    "na",
    "n.a.",
    "null",
    "none",
    "nil",
    "nan",
    "无",
    "暂无",
    "缺失",
    "未提供",
    "未填",
    "不适用",
    "？",
    "?",
}


# ---------------------------------------------------------------------------
# 1. 基础工具
# ---------------------------------------------------------------------------


def _as_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, Decimal):
        return format(v, "f")
    return str(v)


def _headers(t: Any) -> List[str]:
    return [_as_text(h) for h in (getattr(t, "headers", None) or [])]


def _rows(t: Any) -> List[Any]:
    return list(getattr(t, "rows", None) or [])


def _cell(row: Any, idx: int) -> str:
    try:
        length = len(row)
    except TypeError:
        return ""
    if idx < 0 or idx >= length:
        return ""
    return _as_text(row[idx])


def _table_name(t: Any) -> str:
    name = _as_text(getattr(t, "name", "")) or _as_text(getattr(t, "source", ""))
    return name or "未命名表"


def _norm_text(s: str) -> str:
    """列名比较用：去掉全部空白字符。"""
    return _WS_RE.sub("", s or "")


def _norm_key(s: str) -> str:
    """键比较用：去掉全部空白字符（"" 表示空键，不参与关联）。"""
    return _WS_RE.sub("", s or "")


def _is_missing_token(raw: str) -> bool:
    return _norm_text(raw).lower() in _MISSING_TOKENS or raw.strip().lower() in _MISSING_TOKENS


def _fmt_num(d: Optional[Decimal]) -> str:
    """最小化小数表示（200.00 -> 200，0.010 -> 0.01），永不使用科学计数法。"""
    if d is None:
        return "-"
    if not isinstance(d, Decimal):
        d = Decimal(str(d))
    if d == 0:
        return "0"
    s = format(d.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _fmt_money(d: Optional[Decimal]) -> str:
    if d is None:
        return "-"
    if not isinstance(d, Decimal):
        d = Decimal(str(d))
    try:
        q = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:  # pragma: no cover - 极端值兜底
        return _fmt_num(d)
    return "{:,}".format(q)


def _fmt_ratio(d: Optional[Decimal]) -> str:
    if d is None:
        return "-"
    q = d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return _fmt_num(q)


def to_decimal(s: Any) -> Optional[Decimal]:
    """把单元格文本转成 Decimal。

    * ``None`` / ``""`` / ``"-"`` / ``"--"`` / ``"—"`` / ``"/"`` / ``"N/A"`` /
      ``"nan"`` / ``"无"`` 等 -> ``None``（缺失，**绝不是 0**）
    * ``"¥1,234.50"`` -> ``Decimal("1234.50")``；``"1,234.5元"`` -> ``Decimal("1234.5")``
    * ``"（1,234.50）"`` -> ``Decimal("-1234.50")``（会计负数写法）
    * 无法解析时抛 ``ValueError``（中文说明），**不静默当 0**。
    """
    if s is None:
        return None
    if isinstance(s, bool):
        return Decimal(int(s))
    if isinstance(s, Decimal):
        return s + Decimal(0)
    if isinstance(s, int):
        return Decimal(s)
    if isinstance(s, float):  # 避免二进制浮点误差，先转字符串
        return to_decimal(repr(s))

    text = _as_text(s).strip()
    if text == "":
        return None
    if _is_missing_token(text):
        return None

    body = text
    negative = False
    if body.startswith("(") and body.endswith(")"):
        negative = True
        body = body[1:-1]
    if body.startswith("（") and body.endswith("）"):
        negative = True
        body = body[1:-1]

    body = _CURRENCY_RE.sub("", body)
    body = body.replace(",", "").replace("，", "").replace("\u3000", "")
    body = body.replace(" ", "").replace("%", "").replace("+", "")
    if body in ("", "-", ".", "。"):
        return None

    try:
        value = Decimal(body)
    except InvalidOperation:
        # 只有真正解析不出来时，才判断它是不是日期/时间文本（避免误伤 1000.00 这类金额）
        if _DATE_LIKE_RE.search(text):
            raise ValueError("无法把日期时间文本当作金额解析：%r" % (text,))
        matches = _NUM_RE.findall(body)
        if len(matches) != 1:
            raise ValueError(
                "无法解析为金额的单元格内容：%r（请检查该列是否混入了文本或日期）" % (text,)
            )
        head = body.index(matches[0])
        around = body[:head] + body[head + len(matches[0]):]
        if re.search(r"[A-Za-z]", around):
            raise ValueError(
                "无法解析为金额的单元格内容：%r（含字母，可能是编号/编码列，不能当金额累加）" % (text,)
            )
        if re.search(r"[万亿百千]", around):
            raise ValueError(
                "金额含中文数量单位（万/亿/百/千）：%r，请先在数据层换算成元后再对账" % (text,)
            )
        value = Decimal(matches[0])

    if negative:
        value = -value
    return value + Decimal(0)  # 归一化 -0 -> 0


def _safe_dec(raw: Any) -> Optional[Decimal]:
    """容错版 to_decimal：无法解析时返回 None（用于汇总，不让脏数据打断整份对账）。"""
    try:
        return to_decimal(raw)
    except ValueError:
        return None


def _as_tolerance(tolerance: Any) -> Decimal:
    tol = to_decimal(tolerance) if not isinstance(tolerance, Decimal) else tolerance
    if tol is None:
        raise ValueError("容差不能为空，请输入数字，例如 0.01")
    if tol < 0:
        raise ValueError("容差不能为负数：%s" % (_fmt_num(tol),))
    return tol


def column_index(t: Any, name: str) -> int:
    """按列名定位列下标。

    1. 精确匹配表头；2. 去掉全部空白字符后匹配；3. 忽略大小写再匹配一次。
    找不到（或匹配到多列）时抛**中文** KeyError。
    """
    headers = _headers(t)
    target = _as_text(name)
    if target in headers:
        return headers.index(target)

    want = _norm_text(target)
    if want == "":
        raise KeyError("要查找的列名为空（表《%s》）" % _table_name(t))

    hits = [i for i, h in enumerate(headers) if _norm_text(h) == want]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise KeyError(
            "列名 %r 在表《%s》中去空格后匹配到多列：%s；请改用完整列名"
            % (target, _table_name(t), "、".join(headers[i] for i in hits))
        )

    low = want.lower()
    hits = [i for i, h in enumerate(headers) if _norm_text(h).lower() == low]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise KeyError(
            "列名 %r 在表《%s》中忽略大小写后匹配到多列：%s；请改用完整列名"
            % (target, _table_name(t), "、".join(headers[i] for i in hits))
        )

    raise KeyError(
        "表《%s》中找不到列 %r；现有列：%s"
        % (_table_name(t), target, "、".join(headers) if headers else "（无表头）")
    )


# ---------------------------------------------------------------------------
# 2. 键统计与关系判定
# ---------------------------------------------------------------------------


def key_multiplicity(t: Any, key_col: str) -> Dict[str, int]:
    """每个键（去空白后）出现的行数。

    键的空白字符被忽略（前后空格、全角空格都算同一个键），键按原始文本去空白后返回。
    空键（""）也会出现在结果里，便于识别"键缺失(空)"的行。
    """
    idx = column_index(t, key_col)
    counter = Counter(_norm_key(_cell(row, idx)) for row in _rows(t))
    return OrderedDict(counter)


def _is_unique_mult(mult: Any) -> bool:
    if mult is None:
        return True
    if isinstance(mult, int):
        return mult <= 1
    if isinstance(mult, (list, tuple, set)):
        return all(int(v) <= 1 for v in mult)
    if isinstance(mult, dict):
        return all(int(v) <= 1 for v in mult.values())
    return True


def classify_relationship(mult_left: Any, mult_right: Any) -> str:
    """依据"左表键是否唯一 / 右表键是否唯一"判定关联关系。

    ================  ================  ==========
    左键唯一          右键唯一          返回
    ================  ================  ==========
    是                是                一对一
    是                否                一对多
    否                是                多对一
    否                否                多对多
    ================  ================  ==========

    空表（没有任何键）视为唯一。
    """
    left_unique = _is_unique_mult(mult_left)
    right_unique = _is_unique_mult(mult_right)
    if left_unique and right_unique:
        return "一对一"
    if left_unique and not right_unique:
        return "一对多"
    if not left_unique and right_unique:
        return "多对一"
    return "多对多"


# ---------------------------------------------------------------------------
# 3. 列统计 / 自动数值列识别
# ---------------------------------------------------------------------------


def _column_stats(rows: Sequence[Any], idx: int) -> Dict[str, Any]:
    total = Decimal("0")
    missing = 0
    bad = 0
    non_blank = 0
    samples: List[str] = []
    for row in rows:
        raw = _cell(row, idx)
        if raw.strip() == "":
            missing += 1
            continue
        non_blank += 1
        try:
            d = to_decimal(raw)
        except ValueError:
            bad += 1
            if len(samples) < 5:
                samples.append(raw)
            continue
        if d is None:
            missing += 1
            continue
        total += d
    parsed = non_blank - bad - missing
    return {
        "total": total,
        "missing": missing,
        "bad": bad,
        "bad_samples": samples,
        "non_blank": non_blank,
        "parsed": parsed,
        "numeric_ratio": (Decimal(parsed) / Decimal(non_blank)) if non_blank else Decimal("0"),
    }


def _looks_like_date_column(name: str, rows: Sequence[Any], idx: int) -> bool:
    if _DATE_COL_NAME_RE.search(name or ""):
        return True
    values = [_cell(r, idx).strip() for r in rows]
    values = [v for v in values if v]
    if not values:
        return False
    hit = 0
    for v in values[:200]:
        digits = v.replace("-", "").replace("/", "").replace(".", "")
        if len(digits) == 8 and digits.isdigit() and "2020" <= digits[:4] <= "2030":
            hit += 1
    return hit * 2 >= len(values[:200])


# 列名像 ID / 编号 / 券码 —— 这类列即使全是数字也**绝不是金额**。
# 不排除它们会造成真实的错误结论：把券码求和会得到 9 亿亿这种数字，
# 报告里就会出现"券码 放大 1.15 倍 → 是（放大）"这种无意义判定。
_ID_LIKE_NAME_RE = re.compile(
    r"(^|[^a-z])(id|ids)($|[^a-z])"
    r"|编号|单号|订单号|券码|核销码|条码|编码|序号|账号|手机|电话"
    r"|门店号|计划号|素材号|商品号|用户号|号$",
    re.IGNORECASE,
)
# 取值形态：全为长纯数字（>=10 位）且几乎都唯一 → 典型的 ID，不是金额
_ID_LIKE_MIN_DIGITS = 10
_ID_LIKE_UNIQUE_RATIO = Decimal("0.8")
# 唯一性比例在样本太少时没有意义：只有 1~2 行时任何取值都是"唯一"的，
# 那样会把一个真实的金额列误判成 ID，反而让放大检测失效。
_ID_LIKE_MIN_VALUES = 3


def _looks_like_id_column(name: str, rows: Sequence[Any], idx: int) -> bool:
    """判断一列是否是 ID/编号类（不应当作金额列参与合计与放大检测）。

    注意这是"宁可不报也不误报"的取舍：**漏掉一个真实金额列**会让放大检测失效，
    **误把 ID 当金额**只会产生噪音。所以只在两个信号都很强时才排除：
      1. 列名命中 ID/编号/券码 等词；或
      2. 取值全为 >=10 位纯数字，且去重比例 >= 0.8（金额极少长成这样）。
    """
    if _ID_LIKE_NAME_RE.search(name or ""):
        return True
    values = []
    for row in rows:
        v = _cell(row, idx).strip()
        if v:
            values.append(v)
    if not values:
        return False
    if len(values) < _ID_LIKE_MIN_VALUES:
        # 样本太少时"唯一性"不可判定：宁可留着也不会让放大检测失效
        return False
    digits = [v for v in values if v.isdigit()]
    if len(digits) != len(values):
        return False
    if any(len(v) < _ID_LIKE_MIN_DIGITS for v in digits):
        return False
    ratio = Decimal(len(set(values))) / Decimal(len(values))
    return ratio >= _ID_LIKE_UNIQUE_RATIO


def _auto_numeric_cols(t: Any, rows: Sequence[Any], exclude: Sequence[int] = (),
                       skipped_out: Optional[List[str]] = None) -> List[str]:
    """启发式挑出"金额/数量"类数值列（占比 >= 60% 且至少 1 个可解析值）。

    会排除键列、日期/时间列（``20240501`` 能被解析成数字，但绝不是金额），
    以及 ID/编号/券码类列（见 :func:`_looks_like_id_column`）。
    被排除的列名会追加到 ``skipped_out``，**必须在报告里说明**，不能静默丢列。
    """
    out: List[str] = []
    headers = _headers(t)
    for idx, h in enumerate(headers):
        if idx in exclude:
            continue
        if _looks_like_date_column(h, rows, idx):
            continue
        if _looks_like_id_column(h, rows, idx):
            if skipped_out is not None:
                skipped_out.append(h)
            continue
        st = _column_stats(rows, idx)
        if st["parsed"] >= 1 and st["numeric_ratio"] >= Decimal("0.6"):
            out.append(h)
    return out


# ---------------------------------------------------------------------------
# 4. 日期 / 键形态辅助（用于未匹配原因分类）
# ---------------------------------------------------------------------------

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y%m%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m",
    "%Y/%m",
    "%Y%m",
    "%m-%d",
    "%Y年%m月%d日",
    "%Y年%m月",
)


def _parse_date(s: str) -> Optional[datetime]:
    text = (s or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _looks_like_date(s: str) -> bool:
    if _parse_date(s) is not None:
        return True
    return bool(_DATE_LIKE_RE.search(s or ""))


def _date_range(keys: Sequence[str]) -> Optional[Tuple[str, str]]:
    dates = [d for d in (_parse_date(k) for k in keys) if d is not None]
    if not dates:
        return None
    return (min(dates).strftime("%Y-%m-%d"), max(dates).strftime("%Y-%m-%d"))


def _key_kind(k: str) -> str:
    if k == "":
        return "空"
    if _looks_like_date(k):
        return "日期"
    if k.isdigit():
        return "纯数字"
    if re.search(r"[A-Za-z]", k) and re.search(r"\d", k):
        return "字母数字"
    if re.search(r"[\u4e00-\u9fff]", k):
        return "含中文"
    return "其他"


def _key_profile(keys: Sequence[str], mult: Dict[str, int]) -> Dict[str, Any]:
    kinds = Counter(_key_kind(k) for k in keys)
    non_blank = [k for k in keys if k != ""]
    blank_rows = sum(c for k, c in mult.items() if k == "")
    dup_keys = [k for k, c in mult.items() if c > 1 and k != ""]
    return {
        "键行数": len(keys),
        "键唯一值数": len(mult),
        "空键行数": blank_rows,
        "重复键个数": len(dup_keys),
        "最大重复次数": max(mult.values()) if mult else 0,
        "键形态分布": OrderedDict(kinds.most_common()),
        "日期范围": _date_range(non_blank),
        "样例键": non_blank[:5],
    }


def _classify_unmatched_key(
    key: str, other_kinds: Sequence[str], other_has_date: bool
) -> str:
    """把未匹配键归入：键缺失(空) / 时间范围不同(键不存在于另一表) / 其他。"""
    if key == "" or _is_missing_token(key):
        return "键缺失(空)"
    kind = _key_kind(key)
    if kind == "日期":
        if other_has_date:
            return "时间范围不同(键不存在于另一表)"
        return "其他"
    if not other_kinds:
        return "其他"
    if kind in other_kinds:
        # 形态与另一表一致（同一类键），只是该键不在另一表 -> 典型的时间范围/筛选口径不同
        return "时间范围不同(键不存在于另一表)"
    return "其他"


def _unmatched_block(
    side_keys: Sequence[str],
    side_mult: Dict[str, int],
    other_mult: Dict[str, int],
    other_keys: Sequence[str],
) -> Dict[str, Any]:
    other_kinds = {_key_kind(k) for k in other_keys if k != ""}
    other_has_date = any(_key_kind(k) == "日期" for k in other_keys)
    missing_keys = [k for k in side_mult.keys() if k == "" or k not in other_mult]
    reason_rows: "OrderedDict[str, int]" = OrderedDict(
        (("键缺失(空)", 0), ("时间范围不同(键不存在于另一表)", 0), ("其他", 0))
    )
    details = []
    for k in missing_keys:
        reason = _classify_unmatched_key(k, other_kinds, other_has_date)
        rows = side_mult[k]
        reason_rows[reason] = reason_rows.get(reason, 0) + rows
        details.append(
            {
                "键": k if k != "" else "（空）",
                "行数": rows,
                "原因": reason,
            }
        )
    details.sort(key=lambda d: (-d["行数"], d["键"]))
    unmatched_rows = sum(d["行数"] for d in details)
    return {
        "unmatched_rows": unmatched_rows,
        "unmatched_keys": len(missing_keys),
        "reasons": OrderedDict((k, v) for k, v in reason_rows.items() if v),
        "samples": details[:10],
        "samples_truncated": len(details) > 10,
    }


# ---------------------------------------------------------------------------
# 5. join_audit —— 核心：关联放大审计
# ---------------------------------------------------------------------------


def _pair_value_cols(
    left_cols: List[str],
    right_cols: List[str],
    user_paired: bool,
) -> Tuple[List[Tuple[str, Optional[str]]], List[str]]:
    """返回 (配对列表, 提示列表)。

    显式同时给出 ``left_value_cols`` 与 ``right_value_cols`` 时，按**位置**配对
    （``["订单实收金额"]`` + ``["核销金额"]`` 表示这两列语义对应）。
    """
    notes: List[str] = []
    pairs: List[Tuple[str, Optional[str]]] = []
    if user_paired:
        for i, lc in enumerate(left_cols):
            rc = right_cols[i] if i < len(right_cols) else None
            pairs.append((lc, rc))
        if len(left_cols) != len(right_cols):
            notes.append(
                "左侧金额列 %d 个、右侧金额列 %d 个，数量不一致：已按位置配对前 %d 组，"
                "多出来的列只做单表关联前后对比。"
                % (len(left_cols), len(right_cols), min(len(left_cols), len(right_cols)))
            )
        return pairs, notes

    right_set = {_norm_text(c): c for c in right_cols}
    for lc in left_cols:
        rc = right_set.get(_norm_text(lc))
        pairs.append((lc, rc))
        if rc is None:
            notes.append("左表金额列 %r 在右表中没有同名数值列，只做单表关联前后对比。" % (lc,))
    return pairs, notes


def _amplified_amount(before: Decimal, after: Decimal, tol: Decimal) -> bool:
    """金额是否被放大：按规格 ``after > before * (1 + 容差)``，叠加绝对兜底。"""
    if after <= before:
        return False
    diff = after - before
    if before == 0:
        return diff > _ABS_EPS
    return diff > max(before * tol, _ABS_EPS)


def join_audit(
    left: Any,
    right: Any,
    left_key: str,
    right_key: str,
    left_value_cols: Optional[List[str]] = None,
    right_value_cols: Optional[List[str]] = None,
    tolerance: Decimal = Decimal("0.01"),
) -> dict:
    """关联放大审计：直接关联前 / 后行数与金额合计对比 + 未匹配键分类。

    判定（``verdict``）：

    * ``"失败-禁止使用该结果"``：关联后左表金额列合计 > 关联前该列合计 × (1+容差比例)
      （即"N 行左表记录被匹配到多行右表记录"），或右表金额同样被放大，
      或行数已放大且金额有实质性增加（>0.01，防止小额放大被比例容差放过）。
    * ``"警告"``：行数未放大但存在未匹配记录；或关联关系为多对多。
    * ``"通过"``：全部匹配且金额不放大。
    """
    tol = _as_tolerance(tolerance)
    li = column_index(left, left_key)
    ri = column_index(right, right_key)
    left_rows = _rows(left)
    right_rows = _rows(right)

    lkeys = [_norm_key(_cell(r, li)) for r in left_rows]
    rkeys = [_norm_key(_cell(r, ri)) for r in right_rows]
    l_mult = OrderedDict(Counter(lkeys))
    r_mult = OrderedDict(Counter(rkeys))
    relationship = classify_relationship(l_mult, r_mult)

    # 空键不参与关联：否则所有空键会互相匹配（虚假关联）
    l_lookup = OrderedDict((k, c) for k, c in l_mult.items() if k != "")
    r_lookup = OrderedDict((k, c) for k, c in r_mult.items() if k != "")

    left_skipped: List[str] = []
    right_skipped: List[str] = []
    left_cols = list(left_value_cols) if left_value_cols else _auto_numeric_cols(
        left, left_rows, exclude=(li,), skipped_out=left_skipped
    )
    left_cols_auto = not left_value_cols
    right_cols = list(right_value_cols) if right_value_cols else _auto_numeric_cols(
        right, right_rows, exclude=(ri,), skipped_out=right_skipped
    )
    right_cols_auto = not right_value_cols
    notes: List[str] = []
    for tbl, skipped in ((left, left_skipped), (right, right_skipped)):
        if skipped:
            notes.append(
                "表《%s》的列 %s 疑似 ID/编号/券码，**已排除出金额列**（把 ID 求和会产生无意义数字）。"
                "若其中确有金额列，请用 --left-values/--right-values 显式指定。"
                % (_table_name(tbl), "、".join(skipped))
            )
    if left_cols_auto and left_cols:
        notes.append(
            "左表金额列未指定，已自动识别为：%s（若语义不符请用 --left-values 显式指定）"
            % ("、".join(left_cols),)
        )
    if right_cols_auto and right_cols:
        notes.append(
            "右表金额列未指定，已自动识别为：%s（若语义不符请用 --right-values 显式指定）"
            % ("、".join(right_cols),)
        )
    for h, rows in ((left, left_rows), (right, right_rows)):
        for col in _auto_numeric_cols(h, rows):
            idx = column_index(h, col)
            st = _column_stats(rows, idx)
            if st["bad"]:
                notes.append(
                    "表《%s》列 %r 有 %d 个单元格无法解析为金额（例如 %s），已按缺失处理。"
                    % (_table_name(h), col, st["bad"], "、".join(st["bad_samples"][:3]))
                )

    # ---- 关联前 ----
    l_stats = OrderedDict((c, _column_stats(left_rows, column_index(left, c))) for c in left_cols)
    r_stats = OrderedDict((c, _column_stats(right_rows, column_index(right, c))) for c in right_cols)

    # ---- 关联配对计算（乘法等价于逐对展开，避免物化 m*n 行）----
    matched_left_rows = 0
    matched_left_keys = 0
    for k in l_lookup:
        if r_lookup.get(k, 0) > 0:
            matched_left_keys += 1
    for k in lkeys:
        if k != "" and r_lookup.get(k, 0) > 0:
            matched_left_rows += 1
    matched_rows = 0
    for k in lkeys:
        if k != "":
            matched_rows += r_lookup.get(k, 0)

    after_left: "OrderedDict[str, Decimal]" = OrderedDict((c, Decimal("0")) for c in left_cols)
    left_cols_idx = [(c, column_index(left, c)) for c in left_cols]
    right_cols_idx = [(c, column_index(right, c)) for c in right_cols]
    for row, k in zip(left_rows, lkeys):
        mult = r_lookup.get(k, 0) if k != "" else 0
        if mult <= 0:
            continue
        for c, cidx in left_cols_idx:
            v = _safe_dec(_cell(row, cidx))
            if v is not None:
                after_left[c] += v * mult

    after_right: "OrderedDict[str, Decimal]" = OrderedDict((c, Decimal("0")) for c in right_cols)
    for row, k in zip(right_rows, rkeys):
        mult = l_lookup.get(k, 0) if k != "" else 0
        if mult <= 0:
            continue
        for c, cidx in right_cols_idx:
            v = _safe_dec(_cell(row, cidx))
            if v is not None:
                after_right[c] += v * mult

    # ---- 未匹配 ----
    left_unmatched = _unmatched_block(lkeys, l_mult, r_lookup, rkeys)
    right_unmatched = _unmatched_block(rkeys, r_mult, l_lookup, lkeys)

    # ---- 金额放大检测 ----
    row_amplified = matched_rows > matched_left_rows and matched_left_rows > 0
    amplification = []
    fail_reasons: List[str] = []
    right_amplification = []
    for c in left_cols:
        before = l_stats[c]["total"]
        after = after_left[c]
        factor = (after / before) if before != 0 else None
        amp = _amplified_amount(before, after, tol)
        if row_amplified and before != 0 and (after - before) > _ABS_EPS:
            amp = True
        amplification.append(
            {
                "列": c,
                "关联前合计": before,
                "关联后合计": after,
                "放大倍数": factor,
                "放大": amp,
                "行数放大": row_amplified,
            }
        )
    for c in right_cols:
        before = r_stats[c]["total"]
        after = after_right[c]
        factor = (after / before) if before != 0 else None
        amp = _amplified_amount(before, after, tol)
        right_amplification.append(
            {
                "列": c,
                "关联前合计": before,
                "关联后合计": after,
                "放大倍数": factor,
                "放大": amp,
            }
        )

    # 被"一对多"匹配到的行数：这些行会被重复累计
    dup_left_records = sum(1 for k in lkeys if k != "" and r_lookup.get(k, 0) > 1)
    dup_right_records = sum(1 for k in rkeys if k != "" and l_lookup.get(k, 0) > 1)
    matched_right_rows = 0
    for k in rkeys:
        if k != "" and l_lookup.get(k, 0) > 0:
            matched_right_rows += 1

    for item in amplification:
        if item["放大"]:
            fail_reasons.append(
                "关联发生金额放大（%d 行左表记录被匹配到多行右表记录），请先按%s去重汇总后再关联"
                % (dup_left_records, left_key)
            )
            break
    for item in right_amplification:
        if item["放大"]:
            fail_reasons.append(
                "关联发生金额放大（%d 行右表记录被匹配到多行左表记录，右表列 %r 合计由 %s 放大到 %s），"
                "请先按%s聚合后再关联"
                % (
                    dup_right_records,
                    item["列"],
                    _fmt_money(item["关联前合计"]),
                    _fmt_money(item["关联后合计"]),
                    right_key,
                )
            )
            break
    if not fail_reasons and row_amplified:
        fail_reasons.append(
            "关联发生行数放大（%d 行左表记录被匹配到多行右表记录）但金额合计未变，"
            "仍可能把订单级属性错当明细：请先按%s去重汇总后再关联" % (dup_left_records, left_key)
        )

    # ---- 配对列（跨表语义对照）----
    pairs, pair_notes = _pair_value_cols(left_cols, right_cols, bool(left_value_cols and right_value_cols))
    notes.extend(pair_notes)
    pair_comparison = []
    for lc, rc in pairs:
        item = {
            "左列": lc,
            "右列": rc,
            "左列关联前": l_stats[lc]["total"],
            "左列关联后": after_left[lc],
            "放大倍数": (after_left[lc] / l_stats[lc]["total"]) if l_stats[lc]["total"] != 0 else None,
        }
        if rc is not None:
            item["右列关联前"] = r_stats[rc]["total"]
            item["右列关联后"] = after_right[rc]
            ratio_before = (
                (r_stats[rc]["total"] / l_stats[lc]["total"]) if l_stats[lc]["total"] != 0 else None
            )
            ratio_after = (
                (after_right[rc] / after_left[lc]) if after_left[lc] != 0 else None
            )
            item["关联前右/左比率"] = ratio_before
            item["关联后右/左比率"] = ratio_after
            item["比率漂移"] = (
                (ratio_after - ratio_before) if (ratio_before is not None and ratio_after is not None) else None
            )
            if (
                item["比率漂移"] is not None
                and abs(item["比率漂移"]) > tol
                and not _amplified_amount(l_stats[lc]["total"], after_left[lc], tol)
            ):
                notes.append(
                    "配对列 %r / %r 在关联前后的比率发生变化（%s -> %s）：说明两列并非同一语义口径，"
                    "关联结果不能直接用于金额比较。"
                    % (
                        lc,
                        rc,
                        _fmt_ratio(ratio_before),
                        _fmt_ratio(ratio_after),
                    )
                )
        pair_comparison.append(item)

    # ---- verdict ----
    reasons: List[str] = list(fail_reasons)
    warn_reasons: List[str] = []
    if left_unmatched["unmatched_rows"]:
        warn_reasons.append(
            "左表有 %d 行未匹配到右表（%d 个键）" % (left_unmatched["unmatched_rows"], left_unmatched["unmatched_keys"])
        )
    if right_unmatched["unmatched_rows"]:
        warn_reasons.append(
            "右表有 %d 行未匹配到左表（%d 个键）" % (right_unmatched["unmatched_rows"], right_unmatched["unmatched_keys"])
        )
    if relationship == "多对多":
        warn_reasons.append("关联关系为多对多，两侧键都不唯一，关联结果不可作为金额口径")
    if left_unmatched["reasons"].get("键缺失(空)") or right_unmatched["reasons"].get("键缺失(空)"):
        warn_reasons.append("存在键缺失(空)的行，这些行不参与关联（空键互相匹配会造成虚假关联）")

    if fail_reasons:
        verdict = VERDICT_FAIL
        reasons = list(fail_reasons) + warn_reasons
    elif warn_reasons:
        verdict = VERDICT_WARN
        reasons = warn_reasons
    else:
        verdict = VERDICT_PASS
        reasons = ["全部键匹配、金额未放大，关联结果可用"]

    result = {
        "kind": "join_audit",
        "左表": _table_name(left),
        "右表": _table_name(right),
        "左键": left_key,
        "右键": right_key,
        "容差": tol,
        "relationship": relationship,
        "before": {
            "left_rows": len(left_rows),
            "right_rows": len(right_rows),
            "left_values": OrderedDict((c, l_stats[c]["total"]) for c in left_cols),
            "right_values": OrderedDict((c, r_stats[c]["total"]) for c in right_cols),
            "left_cols_auto": left_cols_auto,
            "right_cols_auto": right_cols_auto,
        },
        "after": {
            "rows": matched_rows,
            "left_values": after_left,
            "right_values": after_right,
        },
        "matched_rows": matched_rows,
        "matched_left_rows": matched_left_rows,
        "matched_right_rows": matched_right_rows,
        "unmatched_left": left_unmatched["unmatched_rows"],
        "unmatched_right": right_unmatched["unmatched_rows"],
        "unmatched_left_keys": left_unmatched["unmatched_keys"],
        "unmatched_right_keys": right_unmatched["unmatched_keys"],
        "unmatched_samples_left": left_unmatched["samples"],
        "unmatched_samples_right": right_unmatched["samples"],
        "unmatched_reasons_left": left_unmatched["reasons"],
        "unmatched_reasons_right": right_unmatched["reasons"],
        "key_profile_left": _key_profile(lkeys, l_mult),
        "key_profile_right": _key_profile(rkeys, r_mult),
        "left_value_cols": left_cols,
        "right_value_cols": right_cols,
        "amplification": amplification,
        "right_amplification": right_amplification,
        "pair_comparison": pair_comparison,
        "row_amplification": {
            "matched_rows": matched_rows,
            "matched_left_rows": matched_left_rows,
            "matched_left_keys": matched_left_keys,
            "重复计数左表记录数": dup_left_records,
            "重复计数右表记录数": dup_right_records,
            "行数放大": row_amplified,
        },
        # 便于流水线断言：任一金额列在关联后放大即为 True
        "amplified": bool(
            any(i["放大"] for i in amplification) or any(i["放大"] for i in right_amplification)
        ),
        "verdict": verdict,
        "verdict_reasons": reasons,
        "notes": notes,
        "exit_code": 2 if verdict == VERDICT_FAIL else 0,
    }
    return result


# ---------------------------------------------------------------------------
# 6. aggregate_then_join —— 内置"按对象聚合后再关联"的正确做法
# ---------------------------------------------------------------------------

_AGG_METHODS = ("sum", "max", "min", "first", "last", "count", "nunique", "list")
_AGG_METHOD_CN = {
    "sum": "求和(sum)",
    "max": "取最大值(max)",
    "min": "取最小值(min)",
    "first": "取首个值(first)",
    "last": "取末个值(last)",
    "count": "计数(count)",
    "nunique": "去重计数(nunique)",
    "list": "去重并列(list)",
}
_AGG_REASON = {
    "sum": "金额类列按右表键求和：同一键的多行金额属于同一对象，应累加",
    "max": "数量类列取最大值：一单多券时该列写的是原订单购买总数，逐行相加会按券数翻倍",
    "min": "数量类列取最小值：该列是对象级属性而非逐行明细",
    "first": "非金额非数量列取首个值：订单级属性在核销行中重复出现，不能相加",
    "last": "非金额非数量列取末个值：对象级属性，不能相加",
    "count": "按行数计数",
    "nunique": "标识类列按去重计数（用于统计券数/单数，避免同一券码多行被重复计数）",
    "list": "文本类列去重后并列展示（仅作说明，不参与金额计算）",
}


def _infer_method(col: str) -> str:
    if _QUANTITY_COL_RE.search(col) and not _AMOUNT_COL_RE.search(col):
        return "max"
    if _AMOUNT_COL_RE.search(col):
        return "sum"
    return "first"


class _SimpleTable(object):
    """内部用的鸭子类型表：把聚合结果包成和 contract.Table 一样的属性形状。"""

    def __init__(self, name: str, headers: Sequence[str], rows: Sequence[Sequence[Any]], source: str = "") -> None:
        self.name = name
        self.sheet = ""
        self.headers = list(headers)
        self.rows = [list(r) for r in rows]
        self.source = source or name
        self.n_rows = len(self.rows)
        self.n_cols = len(self.headers)


def _agg_values(method: str, raws: Sequence[str]) -> Any:
    method = method.lower()
    if method not in _AGG_METHODS:
        raise ValueError(
            "不支持的聚合方式 %r；可用：%s" % (method, "、".join(_AGG_METHODS))
        )
    if method == "first":
        for r in raws:
            if r.strip() != "" and not _is_missing_token(r):
                return r
        return ""
    if method == "last":
        for r in reversed(list(raws)):
            if r.strip() != "" and not _is_missing_token(r):
                return r
        return ""
    if method == "count":
        return len(raws)
    if method == "list":
        seen: "OrderedDict[str, int]" = OrderedDict()
        for r in raws:
            if r.strip() != "" and not _is_missing_token(r):
                seen[r.strip()] = 1
        return "/".join(seen.keys())
    if method == "nunique":
        seen2: "OrderedDict[str, int]" = OrderedDict()
        for r in raws:
            if r.strip() != "" and not _is_missing_token(r):
                seen2[_norm_key(r)] = 1
        return len(seen2)

    vals = []
    for r in raws:
        try:
            d = to_decimal(r)
        except ValueError:
            continue
        if d is not None:
            vals.append(d)
    if not vals:
        return ""
    if method == "sum":
        total = Decimal("0")
        for v in vals:
            total += v
        return total
    if method == "max":
        return max(vals)
    return min(vals)


def _aggregate_table(
    t: Any, key_col: str, plan: "OrderedDict[str, str]", suffix: str
) -> Tuple[Any, Dict[str, Any]]:
    ki = column_index(t, key_col)
    headers = _headers(t)
    rows = _rows(t)
    groups: "OrderedDict[str, dict]" = OrderedDict()
    for row in rows:
        k = _norm_key(_cell(row, ki))
        g = groups.get(k)
        if g is None:
            g = {"key": _cell(row, ki).strip(), "rows": [], "count": 0}
            groups[k] = g
        g["rows"].append(row)
        g["count"] += 1

    out_rows: List[List[str]] = []
    for k, g in groups.items():
        vals: List[str] = []
        for idx, h in enumerate(headers):
            if idx == ki:
                vals.append(g["key"])
                continue
            method = plan.get(h, "first")
            v = _agg_values(method, [_cell(r, idx) for r in g["rows"]])
            vals.append(v if isinstance(v, str) else _as_text(v))
        out_rows.append(vals)

    plan_report = []
    for idx, h in enumerate(headers):
        method = "first" if idx == ki else plan.get(h, "first")
        if idx == ki:
            reason = "键列，取原值"
        elif method == "first" and _IDENT_COL_RE.search(h):
            reason = (
                "标识类列取首个值：该列只作展示；若要统计券数/单数，请用去重计数"
                "（例如 --right-agg %s=nunique），绝不能按行相加" % (h,)
            )
        else:
            reason = _AGG_REASON.get(method, "")
        entry = {"列": h, "方式": method, "方式说明": _AGG_METHOD_CN.get(method, method), "理由": reason}
        if _QUANTITY_COL_RE.search(h) and not _AMOUNT_COL_RE.search(h):
            entry["提示"] = _QUANTITY_HINT
        plan_report.append(entry)
    meta = {
        "聚合前行数": len(rows),
        "聚合后行数": len(out_rows),
        "唯一键数": len(groups),
        "键重复的组数": sum(1 for g in groups.values() if g["count"] > 1),
        "最大组内行数": max((g["count"] for g in groups.values()), default=0),
    }
    return _SimpleTable("%s%s" % (_table_name(t), suffix), headers, out_rows, source=_table_name(t)), {
        "meta": meta,
        "plan": plan_report,
    }


def _parse_agg_plan(spec: Any, headers: Sequence[str]) -> Optional["OrderedDict[str, str]"]:
    if spec is None:
        return None
    plan: "OrderedDict[str, str]" = OrderedDict()
    items: List[str] = []
    if isinstance(spec, dict):
        for k, v in spec.items():
            plan[str(k)] = str(v).lower()
        return plan
    if isinstance(spec, str):
        items = [p for p in re.split(r"[,\s]+", spec) if p]
    else:
        for chunk in spec:
            items.extend([p for p in re.split(r"[,\s]+", str(chunk)) if p])
    for item in items:
        if "=" not in item:
            raise ValueError(
                "聚合规则写法错误：%r，应形如 核销金额=sum 购买数量=max 券码=nunique" % (item,)
            )
        col, method = item.split("=", 1)
        col = col.strip()
        method = method.strip().lower()
        if method not in _AGG_METHODS:
            raise ValueError(
                "聚合方式 %r 不支持；可用：%s" % (method, "、".join(_AGG_METHODS))
            )
        plan[col] = method
    return plan


def _key_metric_label(col: str) -> str:
    c = col or ""
    if "订单" in c or "单号" in c or c.lower() in ("order_id", "orderid", "order"):
        return "订单数"
    if "券" in c:
        return "券数"
    if "门店" in c or "店铺" in c or "店" in c:
        return "门店数"
    if "商品" in c or "sku" in c.lower():
        return "商品数"
    if "用户" in c or "顾客" in c or "会员" in c:
        return "用户数"
    return "%s唯一值数" % c


def _count_metric_label(col: str) -> str:
    c = col or ""
    if "券码" in c or ("券" in c and "码" in c) or "券号" in c:
        return "核销券数"
    if "订单" in c or "单号" in c:
        return "订单数"
    if "商品" in c or "sku" in c.lower():
        return "商品数"
    if "用户" in c or "顾客" in c or "会员" in c:
        return "用户数"
    return "%s去重计数" % c


def aggregate_then_join(
    left: Any,
    right: Any,
    left_key: str,
    right_key: str,
    right_agg: Optional[Dict[str, str]] = None,
    left_value_cols: Optional[List[str]] = None,
    right_value_cols: Optional[List[str]] = None,
    tolerance: Decimal = Decimal("0.01"),
) -> dict:
    """正确做法：**右表先按 right_key 聚合，再与左表 1:1 关联**。

    ``right_agg`` 形如 ``{"核销金额": "sum", "购买数量": "max", "券码": "nunique"}``；
    未给出的列按启发式推断（金额 sum / 数量 max / 其他 first），并在报告中写明
    **每一列用的聚合方式及理由**。

    一单多券标准结果（订单 1 行 200 元、核销 2 行各 100 元、购买数量都写 2）：
    订单数=1、核销券数=2（按券码去重）、核销金额=200。
    """
    tol = _as_tolerance(tolerance)
    l_headers = _headers(left)
    r_headers = _headers(right)
    r_rows = _rows(right)
    l_rows = _rows(left)
    l_mult = key_multiplicity(left, left_key)
    r_mult = key_multiplicity(right, right_key)
    relationship_before = classify_relationship(l_mult, r_mult)

    user_plan = _parse_agg_plan(right_agg, r_headers)
    for col in (user_plan or {}):
        column_index(right, col)  # 早失败 + 中文报错
    plan: "OrderedDict[str, str]" = OrderedDict()
    for h in r_headers:
        if user_plan and h in user_plan:
            plan[h] = user_plan[h]
        else:
            plan[h] = _infer_method(h)

    # 左表若在键上不唯一，先按左键聚合（金额 sum / 其他 first）
    left_unique = all(v <= 1 for v in l_mult.values())
    left_plan: "OrderedDict[str, str]" = OrderedDict()
    for h in l_headers:
        left_plan[h] = _infer_method(h) if not left_unique else "first"
    if left_unique:
        left_table = left
        left_agg_meta = {
            "meta": {"聚合前行数": len(l_rows), "聚合后行数": len(l_rows), "唯一键数": len(l_mult), "键重复的组数": 0, "最大组内行数": 1},
            "plan": [
                {"列": h, "方式": "first", "方式说明": _AGG_METHOD_CN["first"], "理由": "左表键唯一，逐行原样保留"}
                for h in l_headers
            ],
        }
    else:
        left_table, left_agg_meta = _aggregate_table(left, left_key, left_plan, "（按%s聚合后）" % left_key)

    right_table, right_agg_meta = _aggregate_table(right, right_key, plan, "（按%s聚合后）" % right_key)

    inner = join_audit(
        left_table,
        right_table,
        left_key,
        right_key,
        left_value_cols=left_value_cols,
        right_value_cols=right_value_cols,
        tolerance=tol,
    )

    # ---- 指标（订单数 / 券数 / 金额）----
    metrics: "OrderedDict[str, Any]" = OrderedDict()
    metrics[_key_metric_label(left_key)] = len([k for k in l_mult if k != ""])
    for h in r_headers:
        method = plan.get(h, "first")
        if method == "sum":
            total = Decimal("0")
            idx = column_index(right_table, h)
            for row in _rows(right_table):
                v = _safe_dec(_cell(row, idx))
                if v is not None:
                    total += v
            metrics[h] = total
        elif method in ("max", "min", "first", "last") and _QUANTITY_COL_RE.search(h):
            idx = column_index(right_table, h)
            vals = [v for v in (_safe_dec(_cell(row, idx)) for row in _rows(right_table)) if v is not None]
            if vals:
                if all(v == vals[0] for v in vals):
                    metrics["%s（按 %s 取）" % (h, method)] = vals[0]
                else:
                    metrics["%s（按 %s 取，各键取值不同）" % (h, method)] = "%s ~ %s（共 %d 个键口径）" % (
                        _fmt_num(min(vals)),
                        _fmt_num(max(vals)),
                        len(vals),
                    )
    ident_cols = [h for h in r_headers if _IDENT_COL_RE.search(h)][:5]
    for h in ident_cols:
        idx = column_index(right, h)
        seen = set()
        for row in r_rows:
            raw = _cell(row, idx)
            if raw.strip() != "" and not _is_missing_token(raw):
                seen.add(_norm_key(raw))
        metrics[_count_metric_label(h)] = len(seen)

    # ---- 聚合前后合计对照 ----
    value_checks = []
    for h in r_headers:
        method = plan.get(h, "first")
        if method in ("count", "nunique", "list"):
            continue
        idx = column_index(right, h)
        before = _column_stats(r_rows, idx)["total"]
        after = _column_stats(_rows(right_table), column_index(right_table, h))["total"]
        if before == 0 and after == 0:
            continue
        value_checks.append(
            {
                "列": h,
                "聚合方式": method,
                "聚合前合计": before,
                "聚合后合计": after,
                "差额": after - before,
                "说明": "聚合方式为 sum，合计应保持不变"
                if method == "sum"
                else "聚合方式为 %s，合计有意不保持（该列是对象级属性）：这正是不能逐行相加的原因" % method,
            }
        )

    warnings: List[str] = []
    quantity_cols = [h for h in r_headers if _QUANTITY_COL_RE.search(h) and not _AMOUNT_COL_RE.search(h)]
    for h in quantity_cols:
        warnings.append("列 %r：%s" % (h, _QUANTITY_HINT))
        if plan.get(h) == "sum":
            warnings.append(
                "列 %r 被显式指定为 sum：若该列是订单购买总数，合计会按券数翻倍并被重复累计，强烈建议改用 max/first。"
                % (h,)
            )
    if relationship_before != "一对一":
        warnings.append(
            "聚合前两表关系为%s（右表键不唯一或左表键不唯一），必须先聚合才能 1:1 关联；"
            "直接关联会放大金额。" % (relationship_before,)
        )
    if right_agg_meta["meta"]["键重复的组数"]:
        warnings.append(
            "右表有 %d 个%s对应多行（最大 %d 行），已按聚合方案压缩为 1 行/键。"
            % (
                right_agg_meta["meta"]["键重复的组数"],
                right_key,
                right_agg_meta["meta"]["最大组内行数"],
            )
        )

    verdict = inner["verdict"]
    reasons = list(inner["verdict_reasons"])
    if verdict == VERDICT_PASS and any(
        plan.get(h) == "sum" for h in quantity_cols
    ):
        verdict = VERDICT_WARN
        reasons = [w for w in warnings if "被显式指定为 sum" in w] or reasons

    result = dict(inner)
    result.update(
        {
            "kind": "aggregate_then_join",
            "relationship": "一对一",
            "relationship_before_aggregation": relationship_before,
            "left_value_cols": inner["left_value_cols"],
            "right_value_cols": inner["right_value_cols"],
            "aggregation_plan_right": right_agg_meta["plan"],
            "aggregation_meta_right": right_agg_meta["meta"],
            "aggregation_plan_left": left_agg_meta["plan"],
            "aggregation_meta_left": left_agg_meta["meta"],
            "metrics": metrics,
            "value_checks": value_checks,
            "warnings": warnings,
            "verdict": verdict,
            "verdict_reasons": reasons,
            "exit_code": 2 if verdict == VERDICT_FAIL else 0,
        }
    )
    return result


# ---------------------------------------------------------------------------
# 7. group_sums / check_additivity / check_partition
# ---------------------------------------------------------------------------


def group_sums(t: Any, group_col: str, value_cols: List[str]) -> List[dict]:
    """按 group_col 分组，返回各组行数与各金额列合计（Decimal）。

    缺失值（``-`` 等）不计入合计，但按列单独计数上报；分组名按去空白后的文本归组。
    """
    gi = column_index(t, group_col)
    pairs = [(c, column_index(t, c)) for c in value_cols]
    rows = _rows(t)
    acc: "OrderedDict[str, dict]" = OrderedDict()
    norm_seen: Dict[str, set] = {}
    for row in rows:
        raw = _cell(row, gi)
        group = raw.strip()
        bucket = acc.get(group)
        if bucket is None:
            bucket = {
                "group": group,
                "rows": 0,
                "values": OrderedDict((c, Decimal("0")) for c, _ in pairs),
                "missing": OrderedDict((c, 0) for c, _ in pairs),
                "bad": OrderedDict((c, 0) for c, _ in pairs),
                "bad_samples": OrderedDict((c, []) for c, _ in pairs),
                "empty_group": group == "",
            }
            acc[group] = bucket
        bucket["rows"] += 1
        for c, idx in pairs:
            cell = _cell(row, idx)
            try:
                d = to_decimal(cell)
            except ValueError:
                bucket["bad"][c] += 1
                if len(bucket["bad_samples"][c]) < 3:
                    bucket["bad_samples"][c].append(cell)
                continue
            if d is None:
                bucket["missing"][c] += 1
                continue
            bucket["values"][c] += d
        norm = _norm_key(group)
        norm_seen.setdefault(norm, set()).add(group)

    out = []
    for group in sorted(acc.keys(), key=lambda g: (g != "", g)):
        bucket = acc[group]
        item = dict(bucket)
        item["group_index"] = len(out)
        item["only_space_differs"] = len(norm_seen.get(_norm_key(group), set())) > 1
        out.append(item)
    return out


def check_additivity(
    t: Any,
    group_col: str,
    value_cols: List[str],
    total_values: Optional[Dict[str, Decimal]] = None,
    tolerance: Decimal = Decimal("0.01"),
) -> dict:
    """分组回加 vs 总量核对：差 1 分钱也要报出来。

    ``total_values`` 未提供（或某列未提供）时，用**整表合计**作为总量，
    并在报告中注明口径来源。结论为 ``"一致"`` 或 ``"不一致(差 X)"``。
    """
    tol = _as_tolerance(tolerance)
    rows = _rows(t)
    groups = group_sums(t, group_col, value_cols)
    notes: List[str] = []

    totals: "OrderedDict[str, Decimal]" = OrderedDict()
    totals_source: "OrderedDict[str, str]" = OrderedDict()
    provided = dict(total_values or {})
    for c in value_cols:
        if c in provided:
            try:
                d = to_decimal(provided[c])
            except ValueError as exc:
                notes.append("总量 %s=%r 无法解析（%s），已改用整表合计。" % (c, provided[c], exc))
                d = None
            if d is None:
                notes.append("总量 %s 传入的是缺失值，已改用整表合计。" % (c,))
                d = _column_stats(rows, column_index(t, c))["total"]
                totals_source[c] = "整表合计"
            else:
                totals_source[c] = "命令行/调用方提供"
            totals[c] = d
        else:
            totals[c] = _column_stats(rows, column_index(t, c))["total"]
            totals_source[c] = "整表合计"
            notes.append("未提供 %s 的总量，已使用整表合计 %s 作为总量。" % (c, _fmt_money(totals[c])))
    for c in provided:
        if c not in value_cols:
            notes.append("提供的总量列 %r 不在 --values 中，已忽略。" % (c,))

    checks = []
    worst = None
    for c in value_cols:
        group_total = Decimal("0")
        for g in groups:
            group_total += g["values"][c]
        diff = group_total - totals[c]
        # 差额绝对值 < 容差才算一致：默认容差 0.01，即"差 1 分钱也要报出来"
        ok = abs(diff) < tol
        item = {
            "列": c,
            "分组回加": group_total,
            "总量": totals[c],
            "总量来源": totals_source[c],
            "差额": diff,
            "绝对差额": abs(diff),
            "是否一致": ok,
        }
        checks.append(item)
        if not ok and (worst is None or item["绝对差额"] > worst["绝对差额"]):
            worst = item

    group_rows_total = sum(g["rows"] for g in groups)
    table_rows = len(rows)
    row_diff = group_rows_total - table_rows
    empty_group_rows = sum(g["rows"] for g in groups if g["empty_group"])

    if worst is None:
        conclusion = "一致"
        verdict = VERDICT_PASS
        reasons = ["各组按列回加与总量一致（容差 %s 内）" % (_fmt_num(tol),)]
    else:
        conclusion = "不一致(差 %s)" % (_fmt_num(worst["绝对差额"]),)
        verdict = VERDICT_FAIL
        direction = "小于" if worst["差额"] < 0 else "大于"
        reasons = [
            "列 %r 分组回加 %s %s 总量 %s，差额 %s"
            % (worst["列"], _fmt_money(worst["分组回加"]), direction, _fmt_money(worst["总量"]), _fmt_money(worst["差额"]))
        ]

    suspect: List[str] = []
    if row_diff < 0:
        suspect.append(
            "分组行数合计 %d 少于全表 %d 行（差 %d 行）：可能有行未落入任何分组"
            "（筛选口径不同 / 空分组 / 只取了部分分组），请先补全分组明细。"
            % (group_rows_total, table_rows, -row_diff)
        )
    elif row_diff > 0:
        suspect.append(
            "分组行数合计 %d 多于全表 %d 行（多 %d 行）：分组可能不互斥（同一行落入多个分组）"
            "或存在重复累计，请用 check_partition 检查互斥性。"
            % (group_rows_total, table_rows, row_diff)
        )
    if empty_group_rows:
        suspect.append(
            "有 %d 行落在空分组（%s 列为空），这些行容易被漏掉或与空值合计混淆。" % (empty_group_rows, group_col)
        )
    if worst is not None:
        if worst["差额"] < 0:
            suspect.append(
                "各组合计小于总量：可能有未覆盖的行，或某些分组被漏取/被筛掉。"
            )
        else:
            suspect.append(
                "各组合计大于总量：分组可能不互斥或重复累计（例如同一行被计入多个分组、"
                "或与总量口径不同，比如总量已去重而分组未去重）。"
            )
        if not any(g["rows"] == 0 for g in groups):
            suspect.append("所有分组都至少有 1 行，问题更可能出在口径或去重上，而不是漏分组。")

    small_diffs = [
        {"列": it["列"], "差额": it["差额"]}
        for it in checks
        if not it["是否一致"] and it["绝对差额"] <= Decimal("1")
    ]
    if small_diffs:
        suspect.append(
            "存在 %d 个不超过 1 元的小额差额（例如 %s）：常见原因是四舍五入/分位截断口径不同，"
            "应按分位重新汇总，不能「差不多就过」。"
            % (len(small_diffs), "、".join("%s=%s" % (d["列"], _fmt_num(d["差额"])) for d in small_diffs))
        )

    return {
        "kind": "additivity",
        "表": _table_name(t),
        "分组列": group_col,
        "金额列": list(value_cols),
        "容差": tol,
        "分组数": len(groups),
        "分组行数合计": group_rows_total,
        "全表行数": table_rows,
        "行数差额": row_diff,
        "groups": groups,
        "checks": checks,
        "totals": totals,
        "conclusion": conclusion,
        "suspects": suspect,
        "notes": notes,
        "verdict": verdict,
        "verdict_reasons": reasons,
        "exit_code": 2 if verdict == VERDICT_FAIL else 0,
    }


_SUMMARY_LABELS = {
    "合计",
    "总计",
    "汇总",
    "全店",
    "全部",
    "所有",
    "total",
    "sum",
    "整体",
    "小计",
    "综合",
}


def check_partition(t: Any, group_col: str, other_cols: Optional[List[str]] = None) -> dict:
    """检查分组是否互斥。

    * 给出 ``other_cols``（多个候选分组列）时做**行级**检查：同一行在这些列上的
      取值是否一致；不一致 -> "状态类别可能不互斥，不能强行相加"。
      若取值之间存在包含关系（如"北京朝阳店" vs "北京"），视为层级关系单列提示。
    * 只给一个分组列时做**标签级**检查：合计行/汇总行标签、分组名包含关系、
      单元格内多值（"北京/上海"）、仅空格不同的分组名。
    """
    gi = column_index(t, group_col)
    others = [(c, column_index(t, c)) for c in (other_cols or [])]
    rows = _rows(t)
    notes: List[str] = []
    conflict_samples: List[dict] = []
    hierarchy_rows = 0
    conflict_rows = 0

    # ---- 标签级检查（无论是否给 other_cols 都要做）----
    labels = [g["group"] for g in group_sums(t, group_col, [])]
    summary_hits = [lb for lb in labels if _norm_text(lb).lower() in _SUMMARY_LABELS]
    if summary_hits:
        notes.append(
            "分组里出现合计/汇总类标签：%s；这类行不能与其他分组相加（会重复累计）。"
            % ("、".join(summary_hits),)
        )
    if "" in labels:
        notes.append("存在空分组（%s 列为空），与其他分组相加前需明确它是否已被包含。" % group_col)
    label_overlaps = []
    for i, a in enumerate(labels):
        for b in labels[i + 1:]:
            if a and b and a != b and (a in b or b in a):
                label_overlaps.append((a, b))
    for a, b in label_overlaps[:5]:
        notes.append(
            "分组名存在包含关系：%r 与 %r，两者可能不互斥（如「全部」与「北京」），不能强行相加。"
            % (a, b)
        )
    multi_value = []
    for row in rows:
        raw = _cell(row, gi)
        if re.search(r"[/、,，;；|]", raw):
            multi_value.append(raw.strip())
    if multi_value:
        notes.append(
            "有 %d 行的 %s 单元格里写了多个分组值（例如 %s）：这些行会被重复计入多个分组。"
            % (len(multi_value), group_col, "、".join(sorted(set(multi_value))[:3]))
        )
    space_diff = [g["group"] for g in group_sums(t, group_col, []) if g.get("only_space_differs")]
    if space_diff:
        notes.append(
            "存在仅空格不同的分组名（%s），建议统一清洗后再汇总。"
            % ("、".join(repr(x) for x in space_diff[:5]),)
        )

    # ---- 行级交叉验证（仅当给出 other_cols）----
    if others:
        cand_cols = [group_col] + [c for c, _ in others]
        cand_idx = [gi] + [i for _, i in others]
        row_values: List[List[str]] = []
        for row in rows:
            vals = []
            for idx in cand_idx:
                raw = _cell(row, idx).strip()
                vals.append("" if _is_missing_token(raw) else raw)
            row_values.append(vals)

        # 先判断哪些候选列"看起来是同一个维度的不同写法"：
        # 只有当两列在至少一行上取值相同或存在包含关系时，才用它们判断互斥；
        # 否则（如"门店区域"vs"状态"）它们是不同维度，拿来做互斥判断只会误报。
        n_cols = len(cand_cols)
        related_pairs = []
        unrelated_pairs = []
        for a in range(n_cols):
            for b in range(a + 1, n_cols):
                compared = 0
                same = 0
                for vals in row_values:
                    if not vals[a] or not vals[b]:
                        continue
                    compared += 1
                    if vals[a] == vals[b] or vals[a] in vals[b] or vals[b] in vals[a]:
                        same += 1
                if compared and same * 2 >= compared:  # 至少一半的行上取值相同/包含
                    related_pairs.append((a, b))
                elif compared:
                    unrelated_pairs.append((cand_cols[a], cand_cols[b]))
        if unrelated_pairs:
            notes.append(
                "列 %s 之间在所有行上都没有相同/包含取值，看起来是不同的维度，"
                "不用于判断互斥（请传入同一维度的候选分组列，例如 门店 / 门店名称 / 所属门店）。"
                % ("、".join("×".join(p) for p in unrelated_pairs[:5]),)
            )

        for n, vals in enumerate(row_values, start=1):
            values = [(cand_cols[i], vals[i]) for i in range(n_cols) if vals[i]]
            if len({v for _, v in values}) <= 1:
                continue
            conflict_pair = False
            hierarchy_pair = False
            for a, b in related_pairs:
                va, vb = vals[a], vals[b]
                if not va or not vb or va == vb:
                    continue
                if va in vb or vb in va:
                    hierarchy_pair = True
                else:
                    conflict_pair = True
            if conflict_pair:
                if len(conflict_samples) < 10:
                    conflict_samples.append({"行号": n, "取值": OrderedDict(values)})
                conflict_rows += 1
            elif hierarchy_pair:
                hierarchy_rows += 1
            elif unrelated_pairs:
                # 只有"不同维度"的列之间有差异 -> 不算互斥问题
                pass
            else:
                conflict_rows += 1
                if len(conflict_samples) < 10:
                    conflict_samples.append({"行号": n, "取值": OrderedDict(values)})
    else:
        notes.append(
            "未提供 other_cols：没有做行级交叉验证，"
            "无法完全排除「一行落入多个分组」（建议带上候选分组列再跑一次）。"
        )

    label_risk = bool(summary_hits or label_overlaps or multi_value)
    if conflict_rows:
        verdict = "可能不互斥"
        exit_code = 2
        message = "状态类别可能不互斥，不能强行相加"
    elif label_risk:
        verdict = "可能不互斥"
        exit_code = 0
        message = "分组名/取值层面存在不互斥风险（如合计行或包含关系），不能强行相加"
    elif hierarchy_rows and others:
        verdict = "可能互斥（层级关系）"
        exit_code = 0
        message = "同一行在候选分组列上的取值是包含关系（层级），可视为互斥，但不能跨层相加"
    else:
        verdict = "互斥"
        exit_code = 0
        message = "未发现同一行落入多个分组的情况"

    return {
        "kind": "partition",
        "表": _table_name(t),
        "分组列": group_col,
        "候选分组列": list(other_cols or []),
        "检查行数": len(rows),
        "冲突行数": conflict_rows,
        "层级关系行数": hierarchy_rows,
        "冲突样例": conflict_samples,
        "verdict": verdict,
        "message": message,
        "verdict_reasons": [message] + notes,
        "notes": notes,
        "exit_code": exit_code,
    }


# ---------------------------------------------------------------------------
# 8. dedupe_rows
# ---------------------------------------------------------------------------


def dedupe_rows(t: Any, subset: Optional[List[str]] = None) -> dict:
    """按键列做重复行检查（不修改原表，只报告）。

    典型用法：按 ``券码`` 去重统计核销券数、按 ``订单ID`` 去重统计订单数。
    **注意**：按比明细更粗的键去重会丢行（一单多券时按订单ID去重只剩 1 行），
    金额列不能"先按订单去重再求和"。
    """
    headers = _headers(t)
    keys = list(subset) if subset else list(headers)
    idxs = [(c, column_index(t, c)) for c in keys]
    rows = _rows(t)

    seen: "OrderedDict[Tuple[str, ...], dict]" = OrderedDict()
    for n, row in enumerate(rows, start=1):
        key = tuple(_norm_key(_cell(row, idx)) for _, idx in idxs)
        entry = seen.get(key)
        if entry is None:
            entry = {"key": tuple(_cell(row, idx).strip() for _, idx in idxs), "count": 0, "row_numbers": [], "first_row": row}
            seen[key] = entry
        entry["count"] += 1
        if len(entry["row_numbers"]) < 3:
            entry["row_numbers"].append(n)

    dup_groups = [e for e in seen.values() if e["count"] > 1]
    dup_groups.sort(key=lambda e: (-e["count"], e["key"]))
    samples = [
        {
            "键": " | ".join(v if v != "" else "（空）" for v in e["key"]),
            "重复行数": e["count"],
            "行号": e["row_numbers"],
        }
        for e in dup_groups[:10]
    ]

    value_recon = []
    numeric_cols = _auto_numeric_cols(t, rows)
    for col in numeric_cols:
        idx = column_index(t, col)
        before = _column_stats(rows, idx)["total"]
        after = Decimal("0")
        for e in seen.values():
            v = _safe_dec(_cell(e["first_row"], idx))
            if v is not None:
                after += v
        value_recon.append(
            {
                "列": col,
                "去重前合计": before,
                "去重后合计": after,
                "差额": after - before,
                "说明": "每个键只取首行，其余重复行被丢弃",
            }
        )
    non_numeric_cols = [c for c in keys if c not in numeric_cols]
    for col in non_numeric_cols:
        idx = column_index(t, col)
        before = _column_stats(rows, idx)["total"]
        if before == 0:
            continue
        after = Decimal("0")
        for e in seen.values():
            v = _safe_dec(_cell(e["first_row"], idx))
            if v is not None:
                after += v
        value_recon.append(
            {
                "列": col,
                "去重前合计": before,
                "去重后合计": after,
                "差额": after - before,
                "说明": "每个键只取首行，其余重复行被丢弃",
                "提示": "该列被当作键列去重，去重后合计不再代表金额口径",
            }
        )

    notes: List[str] = []
    if dup_groups:
        notes.append(
            "存在 %d 个重复键（共 %d 行）：这些行在直接求和时会被重复累计。"
            % (len(dup_groups), sum(e["count"] - 1 for e in dup_groups))
        )
    if keys and non_numeric_cols:
        notes.append(
            "按 %s 去重会保留 %d 行（原 %d 行）：若键比明细粗（如按订单ID去重一单多券只留 1 行），"
            "金额列会漏计，正确做法是按券码（明细粒度）汇总金额，再按订单ID关联。"
            % ("、".join(keys), len(seen), len(rows))
        )
    for col in numeric_cols:
        if _QUANTITY_COL_RE.search(col) and not _AMOUNT_COL_RE.search(col):
            st = _column_stats(rows, column_index(t, col))
            if st["total"] != Decimal(len(rows)):
                notes.append(
                    "数量类列 %r 合计 %s ≠ 行数 %d：该列可能是订单级购买总数，"
                    "不能逐行相加，也不能用它统计券数（券数应按券码去重计数）。"
                    % (col, _fmt_num(st["total"]), len(rows))
                )

    metrics: "OrderedDict[str, Any]" = OrderedDict()
    for col, idx in idxs:
        seen_vals = set()
        for row in rows:
            raw = _cell(row, idx)
            if raw.strip() != "" and not _is_missing_token(raw):
                seen_vals.add(_norm_key(raw))
        metrics[_count_metric_label(col)] = len(seen_vals)

    verdict = VERDICT_PASS if not dup_groups else VERDICT_WARN
    reasons = (
        ["按 %s 未发现重复行" % ("、".join(keys),)]
        if not dup_groups
        else ["按 %s 发现 %d 个重复键" % ("、".join(keys), len(dup_groups))]
    )
    return {
        "kind": "dedupe",
        "表": _table_name(t),
        "键列": keys,
        "去重前行数": len(rows),
        "去重后行数": len(seen),
        "重复键个数": len(dup_groups),
        "重复行数": sum(e["count"] - 1 for e in dup_groups),
        "重复键样例": samples,
        "金额列变化": value_recon,
        "metrics": metrics,
        "notes": notes,
        "verdict": verdict,
        "verdict_reasons": reasons,
        "exit_code": 0,
    }


# ---------------------------------------------------------------------------
# 9. Markdown 报告
# ---------------------------------------------------------------------------


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return _fmt_num(obj)
    if isinstance(obj, datetime):
        return obj.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    return str(obj)


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> List[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_as_text(c) for c in r) + " |")
    return out


def _render_join(res: dict) -> List[str]:
    out: List[str] = []
    is_agg = res.get("kind") == "aggregate_then_join"
    out.append("### 关联关系")
    if is_agg:
        out.append(
            "- 聚合前关系：**%s**；聚合后关系：**%s**（按对象聚合后再关联）"
            % (res.get("relationship_before_aggregation", "-"), res.get("relationship", "-"))
        )
    else:
        out.append("- 关联关系：**%s**（左键%s / 右键%s）" % (res["relationship"], res["左键"], res["右键"]))
    out.append(
        "- 左表《%s》%d 行，右表《%s》%d 行；容差 %s"
        % (res["左表"], res["before"]["left_rows"], res["右表"], res["before"]["right_rows"], _fmt_num(res["容差"]))
    )

    if is_agg:
        out.append("")
        out.append("### 聚合方案（右表先按 %s 聚合）" % res["右键"])
        out.extend(
            _table(
                ["列", "聚合方式", "理由", "提示"],
                [
                    [p["列"], p["方式说明"], p["理由"], p.get("提示", "")]
                    for p in res.get("aggregation_plan_right", [])
                ],
            )
        )
        meta = res.get("aggregation_meta_right", {})
        out.append(
            "- 右表 %s 行 -> 聚合后 %s 行（唯一键 %s 个，其中 %s 个键对应多行，最大 %s 行/键）"
            % (
                meta.get("聚合前行数", "-"),
                meta.get("聚合后行数", "-"),
                meta.get("唯一键数", "-"),
                meta.get("键重复的组数", "-"),
                meta.get("最大组内行数", "-"),
            )
        )

    out.append("")
    out.append("### 关联前")
    out.extend(
        _table(
            ["表", "行数", "金额列", "合计"],
            [["左表", res["before"]["left_rows"], c, _fmt_money(v)] for c, v in res["before"]["left_values"].items()]
            + [["右表", res["before"]["right_rows"], c, _fmt_money(v)] for c, v in res["before"]["right_values"].items()],
        )
    )
    out.append("")
    out.append("### 关联后")
    out.extend(
        _table(
            ["口径", "行数", "金额列", "合计"],
            [["关联后", res["after"]["rows"], c, _fmt_money(v)] for c, v in res["after"]["left_values"].items()]
            + [["关联后（右表）", res["after"]["rows"], c, _fmt_money(v)] for c, v in res["after"]["right_values"].items()],
        )
    )

    out.append("")
    out.append("### 匹配与未匹配")
    out.append("- 匹配行数：**%d**（左表匹配 %d 行 / 右表匹配 %d 行）" % (res["matched_rows"], res["matched_left_rows"], res["matched_right_rows"]))
    out.append("- 未匹配：左表 %d 行（%d 个键） / 右表 %d 行（%d 个键）" % (res["unmatched_left"], res["unmatched_left_keys"], res["unmatched_right"], res["unmatched_right_keys"]))
    for side, label in (("left", "左表"), ("right", "右表")):
        reasons = res.get("unmatched_reasons_%s" % side) or {}
        if reasons:
            out.append(
                "- %s 未匹配原因分类：%s"
                % (label, "；".join("%s %d 行" % (k, v) for k, v in reasons.items()))
            )
        samples = res.get("unmatched_samples_%s" % side) or []
        if samples:
            out.append("")
            out.append("%s未匹配键样例（最多 10 个）：" % label)
            out.extend(_table(["键", "行数", "原因分类"], [[s["键"], s["行数"], s["原因"]] for s in samples]))
    for side, label in (("left", "左表"), ("right", "右表")):
        prof = res.get("key_profile_%s" % side) or {}
        if prof:
            out.append(
                "- %s 键画像：键行数 %s、空键行数 %s、重复键 %s 个、最大重复 %s 次、形态 %s、日期范围 %s"
                % (
                    label,
                    prof.get("键行数"),
                    prof.get("空键行数"),
                    prof.get("重复键个数"),
                    prof.get("最大重复次数"),
                    "、".join("%s:%d" % kv for kv in (prof.get("键形态分布") or {}).items()) or "-",
                    ("%s ~ %s" % prof["日期范围"]) if prof.get("日期范围") else "-",
                )
            )

    out.append("")
    out.append("### 金额放大检测")
    rows = []
    for item in res.get("amplification", []):
        rows.append(
            [
                item["列"],
                _fmt_money(item["关联前合计"]),
                _fmt_money(item["关联后合计"]),
                _fmt_ratio(item["放大倍数"]),
                "是（放大）" if item["放大"] else "否",
            ]
        )
    if rows:
        out.extend(_table(["左表金额列", "关联前合计", "关联后合计", "放大倍数", "是否放大"], rows))
    rows_r = [
        [
            item["列"],
            _fmt_money(item["关联前合计"]),
            _fmt_money(item["关联后合计"]),
            _fmt_ratio(item["放大倍数"]),
            "是（放大）" if item["放大"] else "否",
        ]
        for item in res.get("right_amplification", [])
    ]
    if rows_r:
        out.append("")
        out.extend(_table(["右表金额列", "关联前合计", "关联后合计", "放大倍数", "是否放大"], rows_r))
    pairs = [
        [
            p["左列"],
            p.get("右列") or "-",
            _fmt_money(p["左列关联前"]),
            _fmt_money(p["左列关联后"]),
            _fmt_ratio(p["放大倍数"]),
            _fmt_ratio(p.get("关联前右/左比率")),
            _fmt_ratio(p.get("关联后右/左比率")),
        ]
        for p in res.get("pair_comparison", [])
    ]
    if pairs:
        out.append("")
        out.append("配对列语义对照（不同列名时按位置配对）：")
        out.extend(
            _table(["左列", "配对右列", "左列关联前", "左列关联后", "左列放大倍数", "关联前右/左", "关联后右/左"], pairs)
        )
    ra = res.get("row_amplification") or {}
    out.append(
        "- 行数放大：%s（关联后 %s 行 / 匹配到的左表记录 %s 行；重复计数的左表记录 %s 行）"
        % (
            "是" if ra.get("行数放大") else "否",
            ra.get("matched_rows"),
            ra.get("matched_left_rows"),
            ra.get("重复计数左表记录数"),
        )
    )

    if is_agg:
        out.append("")
        out.append("### 聚合后关键指标")
        if res.get("metrics"):
            out.extend(
                _table(
                    ["指标", "数值"],
                    [[k, _fmt_num(v) if isinstance(v, Decimal) else _as_text(v)] for k, v in res["metrics"].items()],
                )
            )
        if res.get("value_checks"):
            out.append("")
            out.append("聚合前后合计对照：")
            out.extend(
                _table(
                    ["列", "聚合方式", "聚合前合计", "聚合后合计", "差额", "说明"],
                    [
                        [c["列"], c["聚合方式"], _fmt_money(c["聚合前合计"]), _fmt_money(c["聚合后合计"]), _fmt_money(c["差额"]), c["说明"]]
                        for c in res["value_checks"]
                    ],
                )
            )
        if res.get("warnings"):
            out.append("")
            for w in res["warnings"]:
                out.append("- 提示：%s" % (w,))

    out.append("")
    out.append("### 判定")
    out.append("**结论：%s**" % res["verdict"])
    for r in res["verdict_reasons"]:
        out.append("- %s" % (r,))
    if res.get("notes"):
        out.append("")
        for n in res["notes"]:
            out.append("- 备注：%s" % (n,))
    return out


def _render_additivity(res: dict) -> List[str]:
    out: List[str] = []
    out.append("### 分组明细（共 %d 组，行数合计 %d / 全表 %d 行）" % (res["分组数"], res["分组行数合计"], res["全表行数"]))
    head = ["分组", "行数"] + list(res["金额列"]) + ["空分组"]
    rows = []
    for g in res["groups"]:
        rows.append(
            [g["group"] if g["group"] != "" else "（空）", g["rows"]]
            + [_fmt_money(g["values"][c]) for c in res["金额列"]]
            + ["是" if g["empty_group"] else ""]
        )
    out.extend(_table(head, rows))
    out.append("")
    out.append("### 分组回加 vs 总量")
    out.extend(
        _table(
            ["金额列", "分组回加", "总量", "总量来源", "差额", "是否一致"],
            [
                [
                    c["列"],
                    _fmt_money(c["分组回加"]),
                    _fmt_money(c["总量"]),
                    c["总量来源"],
                    _fmt_money(c["差额"]),
                    "一致" if c["是否一致"] else "不一致(差 %s)" % _fmt_num(c["绝对差额"]),
                ]
                for c in res["checks"]
            ],
        )
    )
    out.append("")
    out.append("### 结论")
    out.append("**%s**" % res["conclusion"])
    for r in res["verdict_reasons"]:
        out.append("- %s" % (r,))
    if res["suspects"]:
        out.append("")
        out.append("可能缺失或重复的组：")
        for s in res["suspects"]:
            out.append("- %s" % (s,))
    if res.get("notes"):
        out.append("")
        for n in res["notes"]:
            out.append("- 备注：%s" % (n,))
    return out


def _render_partition(res: dict) -> List[str]:
    out: List[str] = []
    out.append(
        "### 互斥性检查（分组列 %s；候选分组列 %s；检查 %d 行）"
        % (res["分组列"], "、".join(res["候选分组列"]) or "未提供", res["检查行数"])
    )
    out.append("- 同一行落入多个分组的冲突行数：**%d**；层级关系行数：%d" % (res["冲突行数"], res["层级关系行数"]))
    if res["冲突样例"]:
        out.extend(
            _table(
                ["行号", "同行取值"],
                [
                    [s["行号"], "；".join("%s=%s" % (k, v) for k, v in s["取值"].items())]
                    for s in res["冲突样例"]
                ],
            )
        )
    out.append("")
    out.append("### 结论")
    out.append("**%s**" % res["verdict"])
    for r in res["verdict_reasons"]:
        out.append("- %s" % (r,))
    return out


def _render_dedupe(res: dict) -> List[str]:
    out: List[str] = []
    out.append("### 去重检查（键列：%s）" % "、".join(res["键列"]))
    out.append("- 去重前 %d 行 -> 去重后 %d 行；重复键 %d 个，多出 %d 行" % (res["去重前行数"], res["去重后行数"], res["重复键个数"], res["重复行数"]))
    if res.get("metrics"):
        out.extend(
            _table(
                ["指标", "数值"],
                [[k, _fmt_num(v) if isinstance(v, Decimal) else _as_text(v)] for k, v in res["metrics"].items()],
            )
        )
    if res["重复键样例"]:
        out.append("")
        out.append("重复键样例（最多 10 个）：")
        out.extend(_table(["键", "重复行数", "行号"], [[s["键"], s["重复行数"], "、".join(str(x) for x in s["行号"])] for s in res["重复键样例"]]))
    if res["金额列变化"]:
        out.append("")
        out.append("金额列去重前后变化：")
        out.extend(
            _table(
                ["列", "去重前合计", "去重后合计", "差额"],
                [[v["列"], _fmt_money(v["去重前合计"]), _fmt_money(v["去重后合计"]), _fmt_money(v["差额"])] for v in res["金额列变化"]],
            )
        )
    if res.get("notes"):
        out.append("")
        for n in res["notes"]:
            out.append("- 备注：%s" % (n,))
    out.append("")
    out.append("### 结论")
    out.append("**%s**" % res["verdict"])
    for r in res["verdict_reasons"]:
        out.append("- %s" % (r,))
    return out


_KIND_TITLE = {
    "join_audit": "关联放大审计",
    "aggregate_then_join": "按对象聚合后再关联（正确做法）",
    "additivity": "分组回加 vs 总量核对",
    "partition": "分组互斥性检查",
    "dedupe": "重复行/重复键检查",
}


def _summary_row(index: int, res: dict) -> List[str]:
    kind = res.get("kind", "unknown")
    title = _KIND_TITLE.get(kind, kind)
    if kind in ("join_audit", "aggregate_then_join"):
        target = "《%s》×《%s》" % (res.get("左表", "-"), res.get("右表", "-"))
        key_num = "关联后 %s 行，未匹配 左%s/右%s" % (res.get("matched_rows"), res.get("unmatched_left"), res.get("unmatched_right"))
        verdict = res.get("verdict", "-")
    elif kind == "additivity":
        target = "《%s》按 %s" % (res.get("表", "-"), res.get("分组列", "-"))
        key_num = "%s 组，结论 %s" % (res.get("分组数"), res.get("conclusion"))
        verdict = res.get("verdict", "-")
    elif kind == "partition":
        target = "《%s》按 %s" % (res.get("表", "-"), res.get("分组列", "-"))
        key_num = "冲突行 %s" % (res.get("冲突行数"),)
        verdict = res.get("verdict", "-")
    elif kind == "dedupe":
        target = "《%s》按 %s" % (res.get("表", "-"), "、".join(res.get("键列", [])))
        key_num = "%s -> %s 行" % (res.get("去重前行数"), res.get("去重后行数"))
        verdict = res.get("verdict", "-")
    else:
        target = "-"
        key_num = "-"
        verdict = res.get("verdict", "-")
    return [str(index), title, target, verdict, key_num]


def reconciliation_report(results: List[dict]) -> str:
    """把若干检查结果渲染成中文 Markdown 报告。"""
    flat: List[dict] = []
    for item in results or []:
        if isinstance(item, dict):
            flat.append(item)
        elif isinstance(item, (list, tuple)):
            for sub in item:
                if isinstance(sub, dict):
                    flat.append(sub)

    lines: List[str] = []
    lines.append("# 抖音来客 / 本地推 经营数据对账校验报告")
    lines.append("")
    lines.append("- 生成时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("- 检查项数量：%d" % len(flat))

    fails = [r for r in flat if r.get("exit_code") == 2 or r.get("verdict") == VERDICT_FAIL]
    warns = [
        r
        for r in flat
        if r not in fails
        and (
            r.get("verdict") == VERDICT_WARN
            or (r.get("kind") == "partition" and r.get("verdict") == "可能不互斥")
        )
    ]
    if fails:
        overall = "失败——存在禁止使用的结果，禁止据此出报告"
    elif warns:
        overall = "警告——存在未匹配/重复/不互斥等风险，需人工确认后再出报告"
    else:
        overall = "通过"
    lines.append("- 总体结论：**%s**" % overall)
    lines.append("")
    lines.append("## 结论汇总")
    lines.append("")
    lines.extend(_table(["#", "检查项", "对象", "结论", "关键数字"], [_summary_row(i, r) for i, r in enumerate(flat, start=1)]))

    for i, res in enumerate(flat, start=1):
        kind = res.get("kind", "unknown")
        lines.append("")
        lines.append("## %d. %s" % (i, _KIND_TITLE.get(kind, kind)))
        lines.append("")
        if kind in ("join_audit", "aggregate_then_join"):
            lines.extend(_render_join(res))
        elif kind == "additivity":
            lines.extend(_render_additivity(res))
        elif kind == "partition":
            lines.extend(_render_partition(res))
        elif kind == "dedupe":
            lines.extend(_render_dedupe(res))
        else:
            lines.append("```json")
            lines.append(json.dumps(res, ensure_ascii=False, indent=2, default=_json_default))
            lines.append("```")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "口径提醒：金额一律按 Decimal 精确累加；缺失值（- / 空 / N/A）视为缺失而非 0；"
        "关联前必须先确认键唯一性，一单多券必须先聚合再 1:1 关联。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 10. CLI
# ---------------------------------------------------------------------------

USAGE_EXAMPLES = """中文用法示例（脚本目录：scripts/）：

  1) 关联放大审计（一单多券场景必跑）
     python3 scripts/reconcile.py join --left 订单.csv --right 核销.csv \\
         --left-key 订单ID --right-key 订单ID \\
         --left-values 订单实收金额 --right-values 核销金额

  2) 正确做法：右表先按订单ID聚合，再与左表 1:1 关联
     python3 scripts/reconcile.py join --left 订单.csv --right 核销.csv \\
         --left-key 订单ID --right-key 订单ID --aggregate \\
         --right-agg 核销金额=sum 购买数量=max 券码=nunique \\
         --left-values 订单实收金额 --right-values 核销金额

  3) 分组回加核对总量（差 1 分钱也要报出来）
     python3 scripts/reconcile.py group --file 门店明细.csv --group 门店 \\
         --values 消耗 成交金额 --total 消耗=7911.96 成交金额=989159.80

  4) 重复行/重复键检查（券码、订单ID）
     python3 scripts/reconcile.py dup --file 核销.csv --keys 券码 订单ID

  5) 分组互斥性检查（给多个候选分组列可做行级交叉验证）
     python3 scripts/reconcile.py partition --file 门店明细.csv \\
         --group 门店 --others 门店区域 门店类型

  6) 输出 JSON（便于流水线断言）
     python3 scripts/reconcile.py dup --file 核销.csv --keys 券码 --json

  退出码：通过=0 / 警告=0 / 失败=2（失败表示禁止用该结果出报告）。
"""


class _ChineseArgumentParser(argparse.ArgumentParser):
    """参数缺失/错误时给出中文提示与中文用法示例。"""

    def error(self, message: str) -> Any:  # type: ignore[override]
        self.print_usage(sys.stderr)
        sys.stderr.write("参数错误：%s\n\n" % (message,))
        sys.stderr.write(USAGE_EXAMPLES)
        raise SystemExit(2)


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="以 JSON 输出（便于流水线断言）",
    )
    parser.add_argument(
        "--tolerance",
        default=argparse.SUPPRESS,
        help="容差：金额差额判定用绝对值，关联放大判定按比例，默认 0.01",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = _ChineseArgumentParser(
        prog="reconcile.py",
        description="抖音来客 / 本地推 经营数据对账校验（关联放大 / 分组回加 / 重复键 / 分组互斥）",
        epilog=USAGE_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", default=False, help="以 JSON 输出（便于流水线断言）")
    parser.add_argument("--tolerance", default="0.01", help="容差，默认 0.01")
    sub = parser.add_subparsers(dest="command", metavar="命令")

    p_join = sub.add_parser("join", help="关联放大审计（可用 --aggregate 走正确做法）", formatter_class=argparse.RawDescriptionHelpFormatter)
    p_join.add_argument("--left", required=True, help="左表文件（订单表）")
    p_join.add_argument("--right", required=True, help="右表文件（核销表）")
    p_join.add_argument("--left-key", required=True, help="左表关联键列名")
    p_join.add_argument("--right-key", required=True, help="右表关联键列名")
    p_join.add_argument("--left-values", nargs="+", default=None, help="左表金额列（可多列，与 --right-values 按位置配对）")
    p_join.add_argument("--right-values", nargs="+", default=None, help="右表金额列（可多列，与 --left-values 按位置配对）")
    p_join.add_argument("--aggregate", action="store_true", help="先按右键聚合再 1:1 关联（正确做法）")
    p_join.add_argument("--right-agg", nargs="*", default=None, help="聚合规则，如 核销金额=sum 购买数量=max 券码=nunique")
    _add_common_options(p_join)

    p_group = sub.add_parser("group", help="分组回加 vs 总量核对", formatter_class=argparse.RawDescriptionHelpFormatter)
    p_group.add_argument("--file", required=True, help="数据文件")
    p_group.add_argument("--group", required=True, help="分组列名")
    p_group.add_argument("--values", nargs="+", required=True, help="金额列（可多列）")
    p_group.add_argument("--total", nargs="*", default=None, help="总量，写法 消耗=7911.96 成交金额=989159.80；也支持只给数字按 --values 顺序对齐")
    _add_common_options(p_group)

    p_dup = sub.add_parser("dup", help="重复行/重复键检查", formatter_class=argparse.RawDescriptionHelpFormatter)
    p_dup.add_argument("--file", required=True, help="数据文件")
    p_dup.add_argument("--keys", nargs="+", default=None, help="键列（可多列，默认用全部列）")
    _add_common_options(p_dup)

    p_part = sub.add_parser("partition", help="分组互斥性检查", formatter_class=argparse.RawDescriptionHelpFormatter)
    p_part.add_argument("--file", required=True, help="数据文件")
    p_part.add_argument("--group", required=True, help="分组列名")
    p_part.add_argument("--others", nargs="*", default=None, help="候选分组列（可多列，用于行级交叉验证）")
    _add_common_options(p_part)

    return parser


def _parse_total_args(items: Optional[Sequence[str]], value_cols: Sequence[str]) -> Dict[str, Decimal]:
    out: Dict[str, Decimal] = OrderedDict()
    if not items:
        return out
    bare_values: List[str] = []
    for item in items:
        if "=" in item:
            col, val = item.split("=", 1)
            out[col.strip()] = to_decimal(val)  # type: ignore[assignment]
        else:
            bare_values.append(item)
    if bare_values:
        if len(bare_values) != len(value_cols):
            raise ValueError(
                "位置对齐的 --total 需要与 --values 数量一致（%d 个数字 vs %d 个金额列），"
                "或改用 列名=数值 的写法，例如 --total 消耗=7911.96 成交金额=989159.80"
                % (len(bare_values), len(value_cols))
            )
        for col, raw in zip(value_cols, bare_values):
            out[col] = to_decimal(raw)  # type: ignore[assignment]
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        ns = parser.parse_args(args)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 2

    if not getattr(ns, "command", None):
        sys.stderr.write("参数缺失：请给出子命令（join / group / dup / partition）。\n\n")
        sys.stderr.write(USAGE_EXAMPLES)
        return 2

    json_mode = bool(getattr(ns, "json", False))
    try:
        tol = _as_tolerance(getattr(ns, "tolerance", "0.01"))
    except ValueError as exc:
        sys.stderr.write("参数错误：%s\n" % (exc,))
        return 2

    def _load(path: str) -> Any:
        if not CONTRACT_AVAILABLE:
            raise RuntimeError(CONTRACT_HINT)
        if not os.path.exists(path):
            raise RuntimeError("找不到文件：%s（请检查路径是否正确）" % (path,))
        return load_table(path)

    try:
        if ns.command == "join":
            left = _load(ns.left)
            right = _load(ns.right)
            if ns.aggregate:
                res = aggregate_then_join(
                    left,
                    right,
                    ns.left_key,
                    ns.right_key,
                    right_agg=ns.right_agg,
                    left_value_cols=ns.left_values,
                    right_value_cols=ns.right_values,
                    tolerance=tol,
                )
            else:
                res = join_audit(
                    left,
                    right,
                    ns.left_key,
                    ns.right_key,
                    left_value_cols=ns.left_values,
                    right_value_cols=ns.right_values,
                    tolerance=tol,
                )
        elif ns.command == "group":
            t = _load(ns.file)
            totals = _parse_total_args(ns.total, ns.values)
            res = check_additivity(t, ns.group, list(ns.values), total_values=totals, tolerance=tol)
        elif ns.command == "dup":
            t = _load(ns.file)
            res = dedupe_rows(t, list(ns.keys) if ns.keys else None)
        elif ns.command == "partition":
            t = _load(ns.file)
            res = check_partition(t, ns.group, list(ns.others) if ns.others else None)
        else:  # pragma: no cover - argparse 已限制取值
            sys.stderr.write("未知命令：%s\n\n" % (ns.command,))
            sys.stderr.write(USAGE_EXAMPLES)
            return 2
    except (KeyError, ValueError, RuntimeError) as exc:
        msg = exc.args[0] if (isinstance(exc, KeyError) and exc.args) else str(exc)
        sys.stderr.write("【错误】%s\n\n" % (msg,))
        sys.stderr.write(USAGE_EXAMPLES)
        return 2

    if json_mode:
        sys.stdout.write(json.dumps(res, ensure_ascii=False, indent=2, default=_json_default) + "\n")
    else:
        sys.stdout.write(reconciliation_report([res]) + "\n")
    return int(res.get("exit_code", 0))


if __name__ == "__main__":
    sys.exit(main())
