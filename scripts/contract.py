#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抖音来客 / 本地推 经营数据统计审计 —— 数据契约体检模块（程序侧）。

核心原则
--------
1. 关键数字由计算程序产生，模型只负责解释和提假设。本模块只做"看病历"的活：
   读全表头、数实际行列数、算时间 min/max、判断一行代表什么候选对象、找候选主键、
   找金额/状态字段、统计缺失与重复、区分"导出时间"与"业务发生时间"。
2. 原始文件只读。本模块绝不写入、绝不覆盖任何原始数据文件。
3. 所有单元格按字符串原文保存；ID 列绝不转成 int/float（避免精度与科学计数法问题）。
4. 金额一律用 decimal.Decimal 累加，禁止 float 累加金额。
5. 所有面向用户的输出文案为中文；程序不能替人断定业务粒度，因此显式输出
   "一行 = ？" 并标注"需人工确认"。

命令行示例
----------
    python3 scripts/contract.py examples/data/demo_orders.csv
    python3 scripts/contract.py examples/data/*.csv --json
    python3 scripts/contract.py 导出.xlsx --sheet "订单明细"

依赖
----
仅标准库；读 .xlsx/.xlsm 时在函数内部延迟 import openpyxl（缺失会给出中文提示）。
不使用 pandas。
"""

from __future__ import annotations

import argparse
import csv
import datetime
import decimal
import io
import json
import os
import re
import sys
import unicodedata
from collections import Counter, OrderedDict, defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "Table",
    "load_table",
    "profile_column",
    "profile_table",
    "format_contract_report",
    "suggest_id_columns",
    "suggest_time_columns",
    "suggest_money_columns",
    "detect_granularity",
    "parse_decimal",
    "parse_datetime",
    "main",
]

VERSION = "1.0.0"

TEXT_EXTS = (".csv", ".tsv", ".txt", ".tab", ".dat")
XLSX_EXTS = (".xlsx", ".xlsm")
XLS_EXTS = (".xls",)

# ---------------------------------------------------------------------------
# 一、基础工具：规范化 / 缺失符号 / 数值 / 时间解析
# ---------------------------------------------------------------------------


def _nfkc(text: str) -> str:
    """全角转半角等 Unicode 规范化。只用于"判断"，不改变原文。"""
    return unicodedata.normalize("NFKC", text)


# 疑似缺失符号（与空字符串、与真实的 "0" 严格分开统计）
_RAW_MISSING_TOKENS = [
    "-",       # 半角连字符
    "--",
    "—",       # em dash
    "–",       # en dash
    "/",
    "N/A",
    "NA",
    "null",
    "NULL",
    "None",
    "无",
    "暂无",
    "未知",
    "不适用",
]

# key = 规范化小写；value = 报告中展示的规范写法
_MISSING_KEY_TO_LABEL: "OrderedDict[str, str]" = OrderedDict()
for _tok in _RAW_MISSING_TOKENS:
    _key = _nfkc(_tok).strip().lower()
    if _key not in _MISSING_KEY_TO_LABEL:
        _MISSING_KEY_TO_LABEL[_key] = _tok
del _tok, _key

# 科学计数法（Excel 把长 ID 转数值后的典型表现）
SCI_NOTATION_RE = re.compile(r"^\d+(\.\d+)?[eE][+-]?\d+$")
# 形如 12345.0（Excel 把整数 ID 当数值后的残留）
FLOAT_TAIL_RE = re.compile(r"^\d+\.0+$")
# 文件名中的日期：2024-08-15 / 2024_08_15 / 20240815
FILENAME_DATE_RE = re.compile(r"(\d{4})[-_.]?(\d{2})[-_.]?(\d{2})")
# 号码类 ID
PURE_DIGITS_RE = re.compile(r"^\d+$")
# 数字（含指数写法，仍用 Decimal 解析，不经过 float）
_NUMBER_RE = re.compile(r"^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?$")

# 时间解析格式（带 T 的 ISO 与常见中文格式都在内）
TIME_FORMATS: Tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y%m%d",
    "%Y年%m月%d日",
    "%Y年%m月%d日 %H:%M:%S",
    "%Y.%m.%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%SZ",
)

# ---------------------------------------------------------------------------
# 二、列名关键词（列类型推断的唯一依据，全部集中在此便于复核）
# ---------------------------------------------------------------------------

TIME_NAME_RE = re.compile(
    r"(时间|日期|date|time|datetime|timestamp|年份|月份|年度|季度)|(?:日|月|年)$", re.I
)
ID_NAME_RE = re.compile(
    r"(?:^|[^a-z0-9])id(?:[^a-z0-9]|$)|编号|单号|券码|核销码|手机|号码|账号|账户|"
    r"(?:门店|计划|素材|店铺|商品|订单|用户|达人|券|活动|任务|视频)号|码$|uuid|uid|openid",
    re.I,
)
MONEY_NAME_RE = re.compile(
    r"金额|元|消耗|花费|实付|应收|退款|成交|收入|成本|预算|结算|佣金|price|amount|gmv|销售额",
    re.I,
)
# 金额排除：命中这些词的列不是金额列（例如"退款状态""退款时间""退款单号"）
MONEY_EXCLUDE_RE = re.compile(
    r"状态|类型|是否|数量|个数|笔数|单数|次数|率|占比|比例|时间|日期|编号|单号|id|备注|说明",
    re.I,
)
STATUS_NAME_RE = re.compile(r"状态|是否|类型|结果|status|state|result", re.I)
QUANTITY_NAME_RE = re.compile(r"数量|个数|笔数|单数|件数|次数|人数|券数|订单数|条数|数$", re.I)

# 粒度推断关键词
ORDER_COL_RE = re.compile(r"订单(号|ID|id|编号|单据号)|^订单$", re.I)
COUPON_COL_RE = re.compile(r"券码|券ID|券id|券号|核销码|卡券码|码$")
AFTERSALE_COL_RE = re.compile(r"(退款|售后|退货).*(单号|编号|ID|id|券码)|(退款|售后)单")
MATERIAL_COL_RE = re.compile(r"素材|视频ID|视频id|作品")
PLAN_COL_RE = re.compile(r"计划|广告|推广|投放|单元")
STORE_COL_RE = re.compile(r"门店|店铺|门店名称|门店ID|门店id|poi", re.I)
EXPORT_TIME_RE = re.compile(r"导出|下载|生成|拉取|统计时间|快照|更新时间")

TODAY = datetime.date.today()


def _missing_label(value: str) -> Optional[str]:
    """若取值是"疑似缺失符号"，返回规范写法；否则返回 None。"""
    if value is None:
        return None
    return _MISSING_KEY_TO_LABEL.get(_nfkc(value).strip().lower())


def parse_decimal(value: str) -> Optional[decimal.Decimal]:
    """把单元格解析成 Decimal；解析失败返回 None。

    绝不使用 float：金额累加必须走 Decimal。
    """
    if value is None:
        return None
    text = _nfkc(str(value)).strip()
    if text == "":
        return None
    negative = False
    if text.startswith("(") and text.endswith(")"):  # 会计写法 (123.45) 表示负数
        negative = True
        text = text[1:-1].strip()
    for junk in ("¥", "￥", "$", "元", "人民币", ",", " ", "'", "\u00a0"):
        text = text.replace(junk, "")
    if text == "" or text.endswith("%"):
        return None
    if not _NUMBER_RE.match(text):
        return None
    try:
        value_dec = decimal.Decimal(text)
    except (decimal.InvalidOperation, ValueError):
        return None
    if not value_dec.is_finite():
        return None
    return -value_dec if negative else value_dec


def parse_datetime(value: str) -> Tuple[Optional[datetime.datetime], Optional[str]]:
    """尝试多种格式解析时间，返回 (datetime, 命中的格式标签)；失败返回 (None, None)。

    绝不静默丢弃失败样本：调用方必须把失败计数与失败样例写进报告。
    注意：10 位/13 位纯数字会按 Unix 时间戳兜底解析，用的是**本机时区**（不是北京时间固定值），
    命中该分支的列会在命中格式里显示「Unix时间戳」，便于人工复核。
    """
    if value is None:
        return None, None
    text = _nfkc(str(value)).strip()
    if text == "":
        return None, None
    for fmt in TIME_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt), fmt
        except ValueError:
            continue
    # ISO8601 兜底（含毫秒 / 时区 / 带 T）
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.datetime.fromisoformat(candidate)
        if 1900 <= parsed.year <= 2200:
            return parsed, "ISO8601"
    except ValueError:
        pass
    # Unix 时间戳兜底（秒 / 毫秒），只在列已被判定为时间列时才会走到
    if re.match(r"^\d{10}$|^\d{13}$", text):
        try:
            seconds = int(text)
            if len(text) == 13:
                seconds //= 1000
            parsed = datetime.datetime.fromtimestamp(seconds)
            if 2000 <= parsed.year <= 2100:
                return parsed, "Unix时间戳"
        except (OverflowError, OSError, ValueError):
            pass
    return None, None


def _date_text(value: datetime.datetime) -> str:
    """时间统一输出成 'YYYY-MM-DD HH:MM:SS'（零点则只输出日期）。"""
    if (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0):
        return value.strftime("%Y-%m-%d")
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _truncate(text: str, limit: int = 40) -> str:
    text = "" if text is None else str(text)
    text = text.replace("\r", " ").replace("\n", "\\n")
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _md_cell(text: Any, limit: int = 40) -> str:
    """把任意文本安全地放进 Markdown 表格单元格。"""
    text = _truncate("" if text is None else str(text), limit)
    return text.replace("|", "\\|")


def _json_default(obj: Any) -> str:
    """JSON 序列化兜底：Decimal 一律用 str()，避免精度丢失。"""
    if isinstance(obj, decimal.Decimal):
        return str(obj)
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    return str(obj)


def _median(values: Sequence[decimal.Decimal]) -> Optional[decimal.Decimal]:
    """中位数（不引入 statistics，保持依赖最小）。"""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    if n % 2 == 1:
        return ordered[n // 2]
    return (ordered[n // 2 - 1] + ordered[n // 2]) / decimal.Decimal(2)


# ---------------------------------------------------------------------------
# 三、加载：Table / load_table
# ---------------------------------------------------------------------------


class Table:
    """一张已加载的表，所有单元格保持字符串原文。"""

    def __init__(
        self,
        name: str,
        sheet: Optional[str],
        headers: List[str],
        rows: List[List[str]],
        source: str,
        encoding: Optional[str] = None,
        notes: Optional[List[str]] = None,
    ) -> None:
        self.name = name
        self.sheet = sheet
        self.headers = headers
        self.rows = rows
        self.source = source
        self.encoding = encoding
        self.notes = list(notes) if notes else []

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return len(self.headers)

    def column(self, idx: int) -> List[str]:
        """按列号取出整列原始字符串。"""
        return [row[idx] for row in self.rows]

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<Table %s sheet=%s %dx%d>" % (self.name, self.sheet, self.n_rows, self.n_cols)


def _decode_bytes(data: bytes, encoding: Optional[str] = None) -> Tuple[str, str]:
    """按候选编码解码字节流，返回 (文本, 实际使用的编码)。"""
    candidates: List[str] = []
    if encoding:
        candidates.append(encoding)
    else:
        candidates.extend(["utf-8-sig", "utf-8", "gbk", "gb18030"])
        # utf-16 只在有 BOM 时尝试，否则会把普通 ASCII 解成乱码
        if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
            candidates.append("utf-16")

    tried: List[str] = []
    for enc in candidates:
        try:
            text = data.decode(enc)
        except (UnicodeDecodeError, LookupError) as exc:
            tried.append("%s(%s)" % (enc, type(exc).__name__))
            continue
        if enc.startswith("gb") and "\ufffd" in text:
            tried.append("%s(乱码)" % enc)
            continue
        return text, enc
    raise ValueError(
        "无法解码文件文本：所有候选编码都失败（%s）。可用 --encoding 显式指定编码。"
        % "、".join(tried)
    )


def _sniff_delimiter(text: str, ext: str) -> str:
    """猜分隔符（.tsv 直接制表符）。"""
    if ext in (".tsv", ".tab"):
        return "\t"
    lines = [ln for ln in text[:8192].splitlines() if ln.strip() != ""][:5]
    if not lines:
        return ","
    best, best_score = ",", -1.0
    for delim in (",", "\t", ";", "|"):
        counts = [ln.count(delim) for ln in lines]
        if counts[0] == 0:
            continue
        stable = sum(1 for c in counts if c == counts[0])
        score = counts[0] + stable * 0.5
        if score > best_score:
            best, best_score = delim, score
    return best


def _find_header_index(raw_rows: List[List[str]]) -> int:
    """定位表头行：跳过全空行；若开头是 1~2 个单元格的标题/说明行，继续向上寻找。"""
    counts = [sum(1 for c in row if str(c).strip() != "") for row in raw_rows[:50]]
    if not counts:
        return 0
    idx = 0
    while idx < len(counts) and counts[idx] == 0:
        idx += 1
    if idx >= len(counts):
        return 0  # 整表为空
    if counts[idx] <= 2:
        max_ne = max(counts)
        if max_ne >= 3:
            threshold = max(3, -(-max_ne * 6 // 10))  # ceil(0.6 * max_ne)
            for j in range(idx, len(counts)):
                if counts[j] >= threshold:
                    return j
    return idx


def _build_table(
    name: str,
    sheet: Optional[str],
    raw_rows: List[List[str]],
    source: str,
    encoding: Optional[str],
    notes: Optional[List[str]] = None,
) -> Table:
    """把二维字符串数组整理成带完整表头的 Table（裁剪尾部全空行列）。"""
    notes = list(notes) if notes else []
    if not raw_rows:
        return Table(name, sheet, [], [], source, encoding, notes + ["文件没有任何可读内容。"])

    header_idx = _find_header_index(raw_rows)
    if header_idx > 0:
        notes.append("表头之前有 %d 行非表格内容（标题/说明行），已跳过。" % header_idx)

    header_raw = [("" if c is None else str(c)) for c in raw_rows[header_idx]]
    body = [[("" if c is None else str(c)) for c in row] for row in raw_rows[header_idx + 1 :]]

    # 去掉尾部整行为空的行
    while body and all(c.strip() == "" for c in body[-1]):
        body.pop()

    # 去掉中间的空行（空行不是数据行，不能算进行数）
    blank_lines = 0
    cleaned: List[List[str]] = []
    for row in body:
        if all(c.strip() == "" for c in row):
            blank_lines += 1
            continue
        cleaned.append(row)
    body = cleaned
    if blank_lines:
        notes.append("文件中另有 %d 个完全空白的行，已剔除（空行不是数据行）。" % blank_lines)

    width = len(header_raw)
    short_rows = 0
    for row in body:
        if len(row) > width:
            width = len(row)
        elif len(row) < width:
            short_rows += 1

    headers: List[str] = []
    for i in range(width):
        head = header_raw[i].strip() if i < len(header_raw) else ""
        if head == "":
            head = "(空列%d)" % (i + 1)
        headers.append(head)

    rows: List[List[str]] = []
    for row in body:
        if len(row) < width:
            row = list(row) + [""] * (width - len(row))
        rows.append(list(row[:width]))

    # 裁剪：尾部"表头也是空列占位 + 该列全空"的列（Excel 使用范围虚高的典型残留）
    trimmed_cols = 0
    while headers and headers[-1].startswith("(空列") and all(
        row[len(headers) - 1].strip() == "" for row in rows
    ):
        headers.pop()
        for row in rows:
            row.pop()
        trimmed_cols += 1
    if trimmed_cols:
        notes.append("已裁剪尾部 %d 个全空且无表头的列（Excel 声明范围通常会虚高）。" % trimmed_cols)
    if short_rows:
        notes.append("有 %d 行的字段数少于表头列数，已按空值补齐（不是缺失，是行本身短）。" % short_rows)

    # 空表头列提示
    blank_headers = [h for h in headers if h.startswith("(空列")]
    if blank_headers:
        notes.append(
            "表头有 %d 个空列（%s），可能是合并单元格的双层表头或 Excel 空列，需人工确认列含义。"
            % (len(blank_headers), "、".join(blank_headers[:5]))
        )
    dup_headers = sorted({h for h, c in Counter(headers).items() if c > 1})
    if dup_headers:
        notes.append("存在重名列（%s），按列号区分，不要按列名取数。" % "、".join(dup_headers[:5]))

    return Table(name, sheet, headers, rows, source, encoding, notes)


def _cell_to_text(value: Any) -> str:
    """把 openpyxl 单元格值转成字符串原文（ID 绝不转 int/float 再格式化）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, datetime.datetime):
        return _date_text(value)
    if isinstance(value, datetime.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return repr(value)
        # 大数保留 repr：会露出科学计数法，正好触发"精度可能已丢失"的高危警告
        if value.is_integer() and abs(value) < 1e16:
            return str(int(value))
        return repr(value)
    return str(value)


def _load_xlsx(path: str, sheet: Optional[str]) -> Tuple[List[List[str]], str, List[str]]:
    """读取 xlsx/xlsm：延迟 import openpyxl，按真实数据边界裁剪。"""
    try:
        import openpyxl  # 延迟导入：只有真的要读 Excel 时才需要它
    except ImportError:
        raise ValueError(
            "需 pip install openpyxl 才能读 .xlsx / .xlsm 文件（当前环境缺少 openpyxl）。"
            "临时替代方案：在 Excel 里把该表另存为 CSV 后再体检。"
        )

    notes: List[str] = []
    try:
        workbook = openpyxl.load_workbook(filename=path, read_only=True, data_only=True)
    except Exception as exc:  # openpyxl 的异常类型较多，统一转成中文提示
        raise ValueError("读取 Excel 失败：%s（文件可能损坏、加密或不是真正的 xlsx）。" % exc)

    try:
        if sheet:
            if sheet not in workbook.sheetnames:
                raise ValueError(
                    "工作表 %r 不存在。该文件包含：%s" % (sheet, "、".join(workbook.sheetnames))
                )
            worksheet = workbook[sheet]
        else:
            worksheet = workbook.worksheets[0]
            if len(workbook.sheetnames) > 1:
                notes.append(
                    "文件含 %d 个工作表（%s），默认只读第一个「%s」；其他工作表请用 --sheet 单独体检。"
                    % (len(workbook.sheetnames), "、".join(workbook.sheetnames), worksheet.title)
                )

        max_row = worksheet.max_row or 0
        max_col = worksheet.max_column or 0
        notes.append(
            "Excel 声明的使用范围为 %d 行 × %d 列（ws.max_row × ws.max_column），"
            "读取后已按真实数据边界裁剪尾部全空行列。" % (max_row, max_col)
        )
        raw: List[List[str]] = []
        for row in worksheet.iter_rows(
            min_row=1, max_row=max_row, max_col=max_col, values_only=True
        ):
            raw.append([_cell_to_text(v) for v in row])
        return raw, worksheet.title, notes
    finally:
        try:
            workbook.close()
        except Exception:
            pass


def load_table(path: str, sheet: Optional[str] = None, encoding: Optional[str] = None) -> Table:
    """加载 CSV/TSV/TXT/XLSX/XLSM 为 Table（只读，原始文件不会被修改）。"""
    if path is None or str(path).strip() == "":
        raise ValueError("文件路径为空。")
    source = os.path.abspath(path)
    if not os.path.exists(source):
        raise ValueError("文件不存在：%s" % source)
    if os.path.isdir(source):
        raise ValueError("这是目录不是文件：%s" % source)

    name = os.path.basename(source)
    ext = os.path.splitext(source)[1].lower()

    if ext in XLSX_EXTS:
        raw, sheet_name, notes = _load_xlsx(source, sheet)
        return _build_table(name, sheet_name, raw, source, None, notes)

    if ext in XLS_EXTS:
        raise ValueError(
            "暂不支持 .xls 旧格式（%s）：请在 Excel 里另存为 .xlsx，或导出为 CSV 后重试。" % name
        )

    with open(source, "rb") as handle:
        data = handle.read()
    if data == b"":
        raise ValueError("文件为空：%s" % source)

    is_text_ext = ext in TEXT_EXTS
    if not is_text_ext and b"\x00" in data[:8192]:
        raise ValueError(
            "文件 %s 看起来是二进制（含 NUL 字节），不是纯文本表。"
            "支持的类型：%s；.xls 请先另存为 .xlsx 或 CSV。"
            % (name, "、".join(TEXT_EXTS + XLSX_EXTS))
        )

    text, used_encoding = _decode_bytes(data, encoding)
    delimiter = _sniff_delimiter(text, ext)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    raw = [[("" if c is None else c) for c in row] for row in reader]

    notes = ['文本编码：%s；识别到的分隔符：%r' % (used_encoding, delimiter)]
    if encoding is None and used_encoding in ("gbk", "gb18030"):
        notes.append("未使用 UTF-8 编码（实际按 %s 解码），请确认没有乱码。" % used_encoding)
    return _build_table(name, None, raw, source, used_encoding, notes)


# ---------------------------------------------------------------------------
# 四、列级体检：profile_column
# ---------------------------------------------------------------------------


def _name_is_time(name: str) -> bool:
    return bool(TIME_NAME_RE.search(name))


def _name_is_id(name: str) -> bool:
    return bool(ID_NAME_RE.search(name))


def _name_is_money(name: str) -> bool:
    if MONEY_EXCLUDE_RE.search(name):
        return False
    return bool(MONEY_NAME_RE.search(name))


def _name_is_status(name: str) -> bool:
    return bool(STATUS_NAME_RE.search(name))


def _name_is_quantity(name: str) -> bool:
    return bool(QUANTITY_NAME_RE.search(name))


def _value_looks_like_time(values: Sequence[str], min_samples: int = 3, ratio: float = 0.8) -> bool:
    """按取值判断是不是时间列（列名猜不出来时的兜底）。"""
    sample = [v for v in list(values)[:200] if v.strip() != "" and _missing_label(v) is None]
    if len(sample) < min_samples:
        return False
    ok = 0
    for value in sample:
        parsed, _fmt = parse_datetime(value)
        if parsed is not None:
            ok += 1
    return ok >= max(min_samples, int(len(sample) * ratio + 0.5))


def profile_column(t: Table, idx: int) -> Dict[str, Any]:
    """体检单列：返回结构化 dict（含时间/ID/金额/状态各自的专项结论）。"""
    if idx < 0 or idx >= t.n_cols:
        raise IndexError("列号 %d 超出范围（本表共 %d 列）。" % (idx, t.n_cols))

    name = t.headers[idx]
    values = t.column(idx)
    n_rows = len(values)

    empty_count = 0
    missing_counter: "Counter[str]" = Counter()
    real_values: List[str] = []  # 既非空、也不是疑似缺失符号的真实取值
    for value in values:
        if value.strip() == "":
            empty_count += 1
            continue
        label = _missing_label(value)
        if label is not None:
            missing_counter[label] += 1
        else:
            real_values.append(value)

    n_nonempty = n_rows - empty_count
    n_missing = sum(missing_counter.values())

    # 唯一值按"全部取值"统计（空字符串也算一个取值），便于主键判断
    unique_all = len(set(values))
    unique_nonempty = len(set(real_values))

    samples: List[str] = []
    for value in real_values:
        if value not in samples:
            samples.append(value)
        if len(samples) >= 3:
            break
    if len(samples) < 3:  # 真实值不够时，把缺失符号也拿来当示例
        for label, _cnt in missing_counter.items():
            if label not in samples:
                samples.append(label)
            if len(samples) >= 3:
                break

    notes: List[str] = []

    # ---------- 类型推断（先看列名，避免把 20240815 这类 ID 当日期） ----------
    name_time = _name_is_time(name)
    name_id = _name_is_id(name)
    name_money = _name_is_money(name)
    name_status = _name_is_status(name)
    name_quantity = _name_is_quantity(name)
    value_time = False

    if name_time:
        kind = "时间"
    elif name_id:
        kind = "ID"
    elif name_money:
        kind = "金额"
    elif name_status:
        kind = "状态"
    else:
        value_time = _value_looks_like_time(values)
        if value_time:
            kind = "时间"
        elif name_quantity:
            kind = "数量"
        else:
            kind = "文本"

    # 导出时间 vs 业务发生时间
    if kind == "时间" and EXPORT_TIME_RE.search(name):
        notes.append(
            "列名含「导出/生成/统计」字样：这是【导出时间/快照时间】，不是业务发生时间，"
            "按它过滤会得到错误的经营口径。"
        )

    time_info: Optional[Dict[str, Any]] = None
    id_info: Optional[Dict[str, Any]] = None
    money_info: Optional[Dict[str, Any]] = None
    status_info: Optional[Dict[str, Any]] = None

    # ---------- 时间列 ----------
    if kind == "时间":
        parsed_list: List[datetime.datetime] = []
        failed_samples: List[str] = []
        failed_count = 0
        format_counter: "Counter[str]" = Counter()
        for value in real_values:
            parsed, fmt = parse_datetime(value)
            if parsed is None:
                failed_count += 1
                if len(failed_samples) < 5 and value not in failed_samples:
                    failed_samples.append(value)
            else:
                parsed_list.append(parsed)
                format_counter[fmt if fmt else "未知格式"] += 1
        time_min = min(parsed_list) if parsed_list else None
        time_max = max(parsed_list) if parsed_list else None
        time_info = {
            "parsed": len(parsed_list),
            "failed": failed_count,
            "failed_samples": failed_samples,
            "min": _date_text(time_min) if time_min else None,
            "max": _date_text(time_max) if time_max else None,
            "span_days": (time_max.date() - time_min.date()).days + 1 if parsed_list else None,
            "formats": dict(format_counter.most_common()),
            "missing_token_excluded": n_missing,
        }
        if failed_count:
            notes.append(
                "有 %d 个取值无法按已知时间格式解析（已保留在失败样例里，没有静默丢弃）：%s"
                % (failed_count, "、".join(_truncate(s, 20) for s in failed_samples))
            )
        if n_missing:
            notes.append(
                "有 %d 个疑似缺失符号（如 -）出现在时间列，已从解析失败中单独统计，不要当作 1970 年。"
                % n_missing
            )
        if time_max and time_max.date() > TODAY:
            notes.append("时间最大值晚于今天，可能是格式误判或数据本身有未来时间，需人工确认。")

    # ---------- ID 列 ----------
    if kind == "ID":
        sci_values = [v for v in real_values if SCI_NOTATION_RE.match(_nfkc(v).strip())]
        float_tail = [v for v in real_values if FLOAT_TAIL_RE.match(_nfkc(v).strip())]
        pure_digits = [v for v in real_values if PURE_DIGITS_RE.match(_nfkc(v).strip())]
        length_counter: "Counter[int]" = Counter(len(_nfkc(v).strip()) for v in pure_digits)
        leading_zero_values = [v for v in pure_digits if _nfkc(v).strip().startswith("0")]
        duplicate_count = n_rows - unique_all
        id_info = {
            "unique": unique_all,
            "unique_nonempty": unique_nonempty,
            "duplicate_count": duplicate_count,
            "sci_notation_count": len(sci_values),
            "sci_notation_samples": sci_values[:3],
            "float_tail_count": len(float_tail),
            "float_tail_samples": float_tail[:3],
            "pure_digit_count": len(pure_digits),
            "digit_lengths": {"%d位" % k: v for k, v in sorted(length_counter.items())},
            "length_inconsistent": len(length_counter) > 1,
            "leading_zero_count": len(leading_zero_values),
            "leading_zero_samples": leading_zero_values[:3],
        }
        if sci_values:
            notes.append(
                "【高危】出现科学计数法取值（%s）：ID 被 Excel 当成数值处理，精度可能已丢失，"
                "无法从本表还原原始 ID，必须重新导出为文本格式。" % "、".join(sci_values[:3])
            )
        if len(length_counter) > 1:
            notes.append(
                "纯数字 ID 长度不一致（%s），前导零可能已被吞掉（例如 000123 变成 123），"
                "跨表关联时会对不上。" % "、".join("%d位×%d" % (k, v) for k, v in sorted(length_counter.items()))
            )
        if leading_zero_values and len(length_counter) > 1:
            notes.append("存在带前导零的 ID，例如：%s" % "、".join(leading_zero_values[:3]))
        if float_tail:
            notes.append(
                "ID 取值形如 xxx.0（%s），说明该列曾被当作数值存储。" % "、".join(float_tail[:3])
            )
        if duplicate_count > 0:
            notes.append("该 ID 列有 %d 个重复值，它不是唯一键。" % duplicate_count)

    # ---------- 金额列 ----------
    if kind == "金额":
        total = decimal.Decimal("0")
        numeric_values: List[decimal.Decimal] = []
        bad_values: List[str] = []
        for value in real_values:
            parsed_value = parse_decimal(value)
            if parsed_value is None:
                bad_values.append(value)
            else:
                numeric_values.append(parsed_value)
                total += parsed_value  # Decimal 累加，绝不用 float
        money_info = {
            "sum": str(total),
            "numeric_count": len(numeric_values),
            "non_numeric_count": len(bad_values),
            "non_numeric_samples": bad_values[:5],
            "negative_count": sum(1 for v in numeric_values if v < 0),
            "zero_count": sum(1 for v in numeric_values if v == 0),
            "min": str(min(numeric_values)) if numeric_values else None,
            "max": str(max(numeric_values)) if numeric_values else None,
            "missing_token_count": n_missing,
            "missing_token_excluded_from_sum": n_missing,
        }
        if bad_values:
            notes.append(
                "有 %d 个取值既不是数字也不是已知缺失符号（%s），它们没有被当作 0 计入合计，请人工确认。"
                % (len(bad_values), "、".join(_truncate(v, 20) for v in bad_values[:5]))
            )
        if n_missing:
            notes.append(
                "有 %d 个疑似缺失符号（如 -）→ 已在合计中排除。缺失 ≠ 0，把它当 0 会低估金额。"
                % n_missing
            )
        if money_info["negative_count"]:
            notes.append("存在 %d 个负数金额（退款/冲正），算净额与算总额口径不同。" % money_info["negative_count"])

    # ---------- 状态列 ----------
    if kind == "状态":
        counter: "Counter[str]" = Counter(real_values)
        status_info = {
            "n_distinct": len(counter),
            "top": [{"value": k, "count": v} for k, v in counter.most_common(10)],
            "other_count": max(0, len(counter) - 10),
            "missing_token_count": n_missing,
        }
        if len(counter) > 10:
            notes.append("状态取值种类较多（%d 种），只列前 10 种，请人工确认取值全集。" % len(counter))

    return {
        "index": idx,
        "position": "第%d列" % (idx + 1),
        "name": name,
        "kind": kind,
        "is_time": kind == "时间",
        "is_id": kind == "ID",
        "is_money": kind == "金额",
        "is_status": kind == "状态",
        "is_quantity": name_quantity or kind == "数量",
        "matched_by": {
            "name_time": name_time,
            "name_id": name_id,
            "name_money": name_money,
            "name_status": name_status,
            "value_time": value_time,
        },
        "n_rows": n_rows,
        "n_nonempty": n_nonempty,
        "n_empty": empty_count,
        "empty_ratio": round(empty_count / n_rows, 4) if n_rows else 0.0,
        "missing_tokens": dict(missing_counter.most_common()),
        "missing_token_total": n_missing,
        "n_unique": unique_all,
        "n_unique_nonempty": unique_nonempty,
        "samples": samples,
        "time": time_info,
        "id": id_info,
        "money": money_info,
        "status": status_info,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 五、建议列（轻量：先按列名，必要时用取值兜底）
# ---------------------------------------------------------------------------


def suggest_time_columns(t: Table) -> List[int]:
    """建议的时间列：先按列名，再按取值的可解析性兜底。"""
    by_name = [i for i, h in enumerate(t.headers) if _name_is_time(h)]
    if by_name:
        return by_name
    fallback: List[int] = []
    for i in range(t.n_cols):
        if _name_is_id(t.headers[i]):
            continue
        if _value_looks_like_time(t.column(i)):
            fallback.append(i)
    return fallback


def suggest_id_columns(t: Table) -> List[int]:
    """建议的 ID 列（按列名关键词）。"""
    return [i for i, h in enumerate(t.headers) if _name_is_id(h)]


def suggest_money_columns(t: Table) -> List[int]:
    """建议的金额列（按列名关键词，并排除状态/时间/编号类列名）。"""
    return [i for i, h in enumerate(t.headers) if _name_is_money(h)]


# ---------------------------------------------------------------------------
# 六、粒度推断：detect_granularity
# ---------------------------------------------------------------------------


def _first_col(t: Table, pattern: "re.Pattern[str]", skip: Optional[Sequence[int]] = None) -> Optional[int]:
    skip = list(skip) if skip else []
    for i, head in enumerate(t.headers):
        if i in skip:
            continue
        if pattern.search(head):
            return i
    return None


def detect_granularity(t: Table) -> Dict[str, Any]:
    """推断"一行是什么"的候选粒度。程序只给候选与证据，永远需要人工确认。"""
    order_idx = _first_col(t, ORDER_COL_RE)
    coupon_idx = _first_col(t, COUPON_COL_RE, skip=[order_idx] if order_idx is not None else None)
    aftersale_idx = _first_col(t, AFTERSALE_COL_RE)
    material_idx = _first_col(t, MATERIAL_COL_RE)
    plan_idx = _first_col(t, PLAN_COL_RE)
    store_idx = _first_col(t, STORE_COL_RE)

    time_idx = None
    for i, head in enumerate(t.headers):
        if _name_is_time(head) and re.search(r"日期|日|date", head, re.I):
            time_idx = i
            break
    if time_idx is None:
        time_idx = _first_col(t, TIME_NAME_RE)

    def _dup(idx: Optional[int]) -> int:
        if idx is None or not t.rows:
            return 0
        return len(t.rows) - len(set(t.column(idx)))

    def _uniq(idx: Optional[int]) -> int:
        if idx is None:
            return 0
        return len(set(t.column(idx)))

    candidates: List[Dict[str, Any]] = []
    evidence: List[str] = []

    order_dup = _dup(order_idx)
    coupon_dup = _dup(coupon_idx)
    rows = t.n_rows

    if order_idx is not None:
        evidence.append(
            "存在订单列「%s」：%d 行 / %d 个不同值，重复 %d 个。"
            % (t.headers[order_idx], rows, _uniq(order_idx), order_dup)
        )
    if coupon_idx is not None:
        evidence.append(
            "存在券列「%s」：%d 行 / %d 个不同值，重复 %d 个。"
            % (t.headers[coupon_idx], rows, _uniq(coupon_idx), coupon_dup)
        )
    if store_idx is not None:
        evidence.append("存在门店列「%s」：%d 个不同门店。" % (t.headers[store_idx], _uniq(store_idx)))
    if time_idx is not None:
        parsed_times = []
        for value in t.column(time_idx):
            parsed, _fmt = parse_datetime(value)
            if parsed is not None:
                parsed_times.append(parsed)
        if parsed_times:
            evidence.append(
                "时间列「%s」覆盖 %s ~ %s（可解析 %d/%d 行）。"
                % (
                    t.headers[time_idx],
                    _date_text(min(parsed_times)),
                    _date_text(max(parsed_times)),
                    len(parsed_times),
                    rows,
                )
            )

    if order_idx is not None and coupon_idx is not None and order_dup > 0:
        candidates.append(
            {
                "granularity": "一行可能是一张券（一单多券）",
                "confidence": "高",
                "reason": "订单列「%s」出现 %d 个重复值，而券列「%s」基本唯一，说明同一订单被拆成了多行。"
                % (t.headers[order_idx], order_dup, t.headers[coupon_idx]),
                "implication": "券行数 ≠ 订单数；按行汇总订单数会偏大，按行汇总「购买数量」会把一单的数量重复计算。",
            }
        )
    if order_idx is not None and order_dup == 0:
        candidates.append(
            {
                "granularity": "一行可能是一笔订单",
                "confidence": "高",
                "reason": "订单列「%s」在一行一值时完全唯一（%d 行全部不同）。" % (t.headers[order_idx], rows),
                "implication": "可把订单列当主键；但若本表是明细表，仍需确认是否存在一个订单多行的情况被去重掉了。",
            }
        )
    if order_idx is not None and order_dup > 0:
        # 去掉"整行完全重复"的行之后再看订单列是否唯一（导出重复 ≠ 一单多行）
        dedup_rows = list(dict.fromkeys(tuple(row) for row in t.rows))
        dedup_order_unique = len(set(row[order_idx] for row in dedup_rows))
        if len(dedup_rows) < rows and dedup_order_unique == len(dedup_rows):
            removed = rows - len(dedup_rows)
            candidates.append(
                {
                    "granularity": "一行可能是一笔订单（但必须先去掉 %d 行整行完全重复的导出重复行）" % removed,
                    "confidence": "高",
                    "reason": "订单列「%s」在整行去重后完全唯一（%d 行全部不同），重复完全来自整行重复。"
                    % (t.headers[order_idx], len(dedup_rows)),
                    "implication": "先去重再算订单数/金额，否则行数、GMV 都会翻倍；去重前后必须留痕。",
                }
            )
            evidence.append(
                "整行去重后剩 %d 行，订单列「%s」在去重后唯一 → 重复是导出重复，不是一单多行。"
                % (len(dedup_rows), t.headers[order_idx])
            )
    if order_idx is not None and order_dup > 0 and coupon_idx is None:
        candidates.append(
            {
                "granularity": "一行可能是订单的一个明细行（不是一笔订单）",
                "confidence": "中",
                "reason": "订单列「%s」有 %d 个重复值，但没有券列，重复原因未知。"
                % (t.headers[order_idx], order_dup),
                "implication": "直接 COUNT(*) 当订单数会偏大，必须先确认重复是导出重复行还是明细拆分。",
            }
        )
    if coupon_idx is not None and order_idx is None:
        candidates.append(
            {
                "granularity": "一行可能是一张券",
                "confidence": "中",
                "reason": "存在券列「%s」但没有订单列，无法判断券与订单的对应关系。" % t.headers[coupon_idx],
                "implication": "没有订单口径时不能把券行数当订单数。",
            }
        )
    if aftersale_idx is not None:
        candidates.append(
            {
                "granularity": "一行可能是一次售后/退款事件",
                "confidence": "中",
                "reason": "存在售后单号列「%s」，其唯一性为 %d/%d 行。" % (t.headers[aftersale_idx], _uniq(aftersale_idx), rows),
                "implication": "退款表与订单表是不同粒度，不能直接相加，需按订单号回连。",
            }
        )
    if material_idx is not None and time_idx is not None and order_idx is None:
        candidates.append(
            {
                "granularity": "一行可能是素材×日聚合",
                "confidence": "中",
                "reason": "同时存在素材列「%s」与时间列「%s」，且没有订单列。"
                % (t.headers[material_idx], t.headers[time_idx]),
                "implication": "是聚合数据，只能做趋势/占比，不能下钻到订单。",
            }
        )
    if plan_idx is not None and time_idx is not None:
        candidates.append(
            {
                "granularity": "一行可能是计划×日聚合",
                "confidence": "中",
                "reason": "同时存在计划/投放列「%s」与时间列「%s」。" % (t.headers[plan_idx], t.headers[time_idx]),
                "implication": "计划维度与订单维度不能跨表直接相加，注意重复计算。",
            }
        )
    if store_idx is not None and time_idx is not None and order_idx is None:
        candidates.append(
            {
                "granularity": "一行可能是门店×日聚合",
                "confidence": "中",
                "reason": "存在门店列「%s」与时间列「%s」，且没有订单列。" % (t.headers[store_idx], t.headers[time_idx]),
                "implication": "是汇总口径，行数 = 门店数×天数（可能还有缺口），不能当订单数。",
            }
        )

    if not candidates:
        candidates.append(
            {
                "granularity": "无法从列名推断粒度",
                "confidence": "低",
                "reason": "既没有订单/券/售后单号，也没有可用的时间与维度列。",
                "implication": "请人工确认一行代表什么，并补充导出字段。",
            }
        )

    if time_idx is not None:
        # 日期不连续 / 每天行数不一致，都会影响"一行"的判断
        span = {}
        for value in t.column(time_idx):
            parsed, _fmt = parse_datetime(value)
            if parsed is not None:
                span[parsed.date()] = span.get(parsed.date(), 0) + 1
        if span:
            rows_per_day = sorted(set(span.values()))
            evidence.append(
                "时间列覆盖 %d 个自然日，每天行数取值集合为 %s。"
                % (len(span), "、".join(str(v) for v in rows_per_day[:5]))
            )
            if len(rows_per_day) > 1:
                evidence.append("每天行数不一致 → 一行不是「日」本身，而是更细的维度组合。")

    return {
        "candidates": candidates,
        "evidence": evidence,
        "needs_human_confirm": True,
    }


# ---------------------------------------------------------------------------
# 七、表级体检：profile_table
# ---------------------------------------------------------------------------


def _extract_filename_date(name: str) -> Optional[str]:
    """从文件名里提取日期（YYYY-MM-DD），无则返回 None。"""
    base = os.path.basename(name or "")
    for match in FILENAME_DATE_RE.finditer(base):
        year, month, day = (int(g) for g in match.groups())
        if 2000 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31:
            try:
                return datetime.date(year, month, day).isoformat()
            except ValueError:
                continue
    return None


def _temporal_checks(t: Table, cols: List[Dict[str, Any]]) -> Dict[str, Any]:
    """日期连续性 + 未结束的部分日。

    只有"看起来是按天一行"的表（每天最多 1 行）才做缺口告警；
    事件明细表（订单/券/退款流水）本身几天没有数据是正常的，不报缺口，只做说明。
    """
    time_cols = [c for c in cols if c["kind"] == "时间" and c["time"] and c["time"]["parsed"] > 0]
    date_col = None
    for c in time_cols:
        if re.search(r"日期|(?:日|date)$", c["name"], re.I):
            date_col = c
            break
    if date_col is None and time_cols:
        date_col = time_cols[0]

    result: Dict[str, Any] = {
        "date_column": date_col["name"] if date_col else None,
        "is_daily_series": False,
        "missing_days": [],
        "missing_day_count": 0,
        "partial_last_day": None,
        "rows_per_day_consistent": None,
        "note": "",
    }
    if date_col is None:
        return result

    idx = date_col["index"]
    per_day: Dict[datetime.date, int] = defaultdict(int)
    for value in t.column(idx):
        parsed, _fmt = parse_datetime(value)
        if parsed is not None:
            per_day[parsed.date()] += 1
    if len(per_day) < 3:
        return result

    days = sorted(per_day)
    result["rows_per_day_consistent"] = len(set(per_day.values())) == 1
    # 事件明细表（有订单号/券码/退款单号这类"一事件一行"的 ID）不做缺口告警；
    # 只有聚合表（没有事件 ID 列，维度键如门店ID不算）才按天核对连续性。
    dimension_key_re = re.compile(r"门店|店铺|商品|素材|计划|活动|达人|视频|渠道|账号|区域")
    has_event_id = any(
        c["kind"] == "ID" and not dimension_key_re.search(c["name"]) for c in cols
    )
    is_daily_series = not has_event_id
    result["is_daily_series"] = is_daily_series

    if not is_daily_series:
        result["note"] = (
            "该表是事件明细（每行一个业务事件编号，同一天最多 %d 行），"
            "日期不连续属于正常现象，不做缺口告警。" % max(per_day.values())
        )
        return result

    missing: List[str] = []
    cursor = days[0]
    while cursor <= days[-1]:
        if cursor not in per_day:
            missing.append(cursor.isoformat())
        cursor += datetime.timedelta(days=1)
    result["missing_days"] = missing[:10]
    result["missing_day_count"] = len(missing)

    money_col = None
    for c in cols:
        if c["kind"] == "金额" and c["money"] and c["money"]["numeric_count"] > 0:
            money_col = c
            break
    if money_col is not None and len(days) >= 4:
        per_day_sum: Dict[datetime.date, decimal.Decimal] = defaultdict(lambda: decimal.Decimal("0"))
        for row in t.rows:
            parsed, _fmt = parse_datetime(row[idx])
            if parsed is None:
                continue
            value = parse_decimal(row[money_col["index"]])
            if value is not None:
                per_day_sum[parsed.date()] += value
        last_day = days[-1]
        others = [per_day_sum[d] for d in days[:-1]]
        base = _median(others)
        if base and base > 0:
            ratio = per_day_sum[last_day] / base
            if ratio < decimal.Decimal("0.5"):
                result["partial_last_day"] = {
                    "date": last_day.isoformat(),
                    "money_column": money_col["name"],
                    "last_day_sum": str(per_day_sum[last_day]),
                    "median_other_days": str(base),
                    "ratio": str(round(ratio, 4)),
                }
    return result


def _build_qa(profiles_bits: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """每个表结尾固定的「本表可回答 / 不能回答」。（各 ≤3 条）"""
    can: List[str] = []
    cannot: List[str] = []
    n_rows = profiles_bits["n_rows"]
    n_cols = profiles_bits["n_cols"]
    time_cols = profiles_bits["time_columns"]
    money_cols = profiles_bits["money_columns"]
    id_cols = profiles_bits["id_columns"]
    granularity = profiles_bits["granularity"]

    if time_cols:
        main_time = time_cols[0]
        can.append(
            "本表 %d 行 × %d 列，可回答「%s」的时间范围：%s ~ %s。"
            % (n_rows, n_cols, main_time["name"], main_time["time"]["min"], main_time["time"]["max"])
        )
    else:
        can.append("本表 %d 行 × %d 列，可回答各列的非空/唯一值分布（本表没有可识别的时间列）。" % (n_rows, n_cols))
    if money_cols:
        money = money_cols[0]
        can.append(
            "可回答「%s」的 Decimal 合计 = %s（已排除 %d 个疑似缺失符号）。"
            % (money["name"], money["money"]["sum"], money["money"]["missing_token_count"])
        )
    if id_cols:
        can.append(
            "可回答 ID 列的唯一性/重复数，例如「%s」唯一值 %d 个。"
            % (id_cols[0]["name"], id_cols[0]["id"]["unique"])
        )

    cannot.append(
        "不能替人断定业务粒度：程序给出的候选是「%s」，必须人工确认。"
        % granularity["candidates"][0]["granularity"]
    )
    cannot.append("不能判断缺失值（空 / - / N/A）在业务上是否等于 0，必须回后台或问业务方。")
    if len(profiles_bits.get("tables_in_scope", [])) > 1:
        cannot.append("不能单独回答跨表口径（订单/券/退款三种粒度不能直接相加），需要按主键回连后再算。")
    else:
        cannot.append("不能回答本表之外的指标（没有其他表的数据，无法验证总额是否与后台一致）。")
    return can[:3], cannot[:3]


def profile_table(t: Table) -> Dict[str, Any]:
    """体检整张表，返回可 JSON 序列化的结构化 dict（关键数字全部由程序算出）。"""
    cols = [profile_column(t, i) for i in range(t.n_cols)]

    time_columns = [c for c in cols if c["kind"] == "时间"]
    id_columns = [c for c in cols if c["kind"] == "ID"]
    money_columns = [c for c in cols if c["kind"] == "金额"]
    status_columns = [c for c in cols if c["kind"] == "状态"]
    text_columns = [c for c in cols if c["kind"] in ("文本", "数量")]

    # 整行完全重复
    row_counter = Counter(tuple(row) for row in t.rows)
    duplicate_row_extra = sum(c - 1 for c in row_counter.values() if c > 1)
    duplicate_row_samples = [
        [str(cell) for cell in row] for row, c in row_counter.items() if c > 1
    ][:3]

    # 候选主键
    exact_keys: List[Dict[str, Any]] = []
    ratio_pool: List[Tuple[float, Dict[str, Any]]] = []
    if t.n_rows > 0:
        for c in cols:
            ratio = c["n_unique"] / float(t.n_rows)
            if c["n_unique"] == t.n_rows:
                exact_keys.append(
                    {
                        "index": c["index"],
                        "name": c["name"],
                        "unique": c["n_unique"],
                        "rows": t.n_rows,
                        "kind": c["kind"],
                    }
                )
            ratio_pool.append((ratio, c))
    top_ratio = [
        {
            "index": c["index"],
            "name": c["name"],
            "unique": c["n_unique"],
            "rows": t.n_rows,
            "ratio": round(ratio, 4),
            "kind": c["kind"],
        }
        for ratio, c in sorted(ratio_pool, key=lambda pair: -pair[0])[:3]
    ]

    granularity = detect_granularity(t)
    temporal = _temporal_checks(t, cols)

    # 全空列
    all_empty_columns = [c["name"] for c in cols if c["n_nonempty"] == 0]

    # 时间范围（多时间列合并）
    global_min = None
    global_max = None
    for c in time_columns:
        if not c["time"]["min"]:
            continue
        if global_min is None or c["time"]["min"] < global_min:
            global_min = c["time"]["min"]
        if global_max is None or c["time"]["max"] > global_max:
            global_max = c["time"]["max"]

    # ---------- 风险提示 ----------
    warnings: List[str] = []

    file_date = _extract_filename_date(t.name)
    filename_date_check: Dict[str, Any] = {
        "filename_date": file_date,
        "data_min": global_min,
        "data_max": global_max,
        "consistent": None,
        "message": "",
    }
    if file_date is None:
        filename_date_check["message"] = "文件名里没有日期，无法用文件名判断数据覆盖范围，请以表内时间列 min/max 为准。"
    elif global_min is None:
        filename_date_check["message"] = (
            "文件名日期为 %s，但表内没有可识别的时间列，无法核对口径。" % file_date
        )
    else:
        low = global_min[:10]
        high = global_max[:10]
        inside = low <= file_date <= high
        filename_date_check["consistent"] = inside
        if inside:
            filename_date_check["message"] = (
                "文件名日期 %s 落在数据时间范围内（%s ~ %s），不构成矛盾；但文件名日期通常只是导出日，"
                "不能当作业务发生日过滤。" % (file_date, low, high)
            )
        else:
            filename_date_check["message"] = (
                "【警告】文件名日期 %s 不等于实际数据日期（表内业务时间是 %s ~ %s）。"
                "必须按表内业务时间列过滤，不能按文件名日期切数据。" % (file_date, low, high)
            )
            warnings.append(filename_date_check["message"])

    for c in cols:
        warnings.extend("%s：%s" % (c["name"], n) for n in c["notes"])

    # 加载阶段发现的结构性问题（重名列 / 空表头列）同样属于取数风险，提升到风险提示
    for note in t.notes:
        if "重名列" in note or "空列" in note:
            warnings.append(note)

    if duplicate_row_extra:
        warnings.append(
            "存在 %d 行「整行完全重复」（叠加在正常行之上），可能是重复导出或重复拼接，"
            "直接求和会翻倍。" % duplicate_row_extra
        )

    excel_range_warning = None
    if any(h.startswith("(空列") for h in t.headers) or all_empty_columns:
        excel_range_warning = (
            "Excel 报告的使用范围可能不准确，已按真实数据边界裁剪；"
            "本表存在空表头列（%s）或整列为空的列（%s），请确认是否漏读/多读列。"
            % (
                "、".join([h for h in t.headers if h.startswith("(空列")][:5]) or "无",
                "、".join(all_empty_columns[:5]) or "无",
            )
        )
        warnings.append(excel_range_warning)

    if temporal["missing_day_count"] and temporal["is_daily_series"]:
        warnings.append(
            "日期不连续：在 %s ~ %s 之间缺 %d 天（%s…）。按天算均值/同比时要把缺口补 0 还是剔除，需人工确认。"
            % (
                (global_min or "")[:10],
                (global_max or "")[:10],
                temporal["missing_day_count"],
                "、".join(temporal["missing_days"][:5]),
            )
        )
    if temporal["partial_last_day"]:
        part = temporal["partial_last_day"]
        warnings.append(
            "【疑似未结束的部分日】%s 的「%s」合计 %s，只有其他天中位数 %s 的 %s 倍，"
            "最后一天很可能是导出时的未完成日（需人工确认）。"
            % (part["date"], part["money_column"], part["last_day_sum"], part["median_other_days"], part["ratio"])
        )

    export_time_cols = [
        c["name"] for c in time_columns if EXPORT_TIME_RE.search(c["name"])
    ]
    if export_time_cols:
        warnings.append(
            "本表含导出/统计时间列（%s）：它与业务发生时间是两回事，审计时必须锁定业务时间列。"
            % "、".join(export_time_cols)
        )

    try:
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(t.source)).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        mtime = None

    profiles_bits = {
        "n_rows": t.n_rows,
        "n_cols": t.n_cols,
        "time_columns": time_columns,
        "money_columns": money_columns,
        "id_columns": id_columns,
        "granularity": granularity,
        "tables_in_scope": [t.name],
    }
    can_answer, cannot_answer = _build_qa(profiles_bits)

    one_row_is = "一行 = ？ （需人工确认）程序候选：%s" % "；".join(
        "%s（置信度%s）" % (c["granularity"], c["confidence"]) for c in granularity["candidates"][:3]
    )

    return {
        "module_version": VERSION,
        "name": t.name,
        "source": t.source,
        "sheet": t.sheet,
        "encoding": t.encoding,
        "file_mtime": mtime,
        "n_rows": t.n_rows,
        "n_cols": t.n_cols,
        "headers": list(t.headers),
        "blank_header_columns": [h for h in t.headers if h.startswith("(空列")],
        "all_empty_columns": all_empty_columns,
        "load_notes": list(t.notes),
        "columns": cols,
        "time_columns": time_columns,
        "id_columns": id_columns,
        "money_columns": money_columns,
        "status_columns": status_columns,
        "text_columns": text_columns,
        "data_time_min": global_min,
        "data_time_max": global_max,
        "candidate_primary_keys": {"exact": exact_keys, "top_ratio": top_ratio},
        "duplicate_rows": {
            "count": duplicate_row_extra,
            "unique_rows": len(row_counter),
            "samples": duplicate_row_samples,
        },
        "granularity": granularity,
        "one_row_is": one_row_is,
        "temporal": temporal,
        "filename_date_check": filename_date_check,
        "excel_range_warning": excel_range_warning,
        "warnings": warnings,
        "qa_can_answer": can_answer,
        "qa_cannot_answer": cannot_answer,
    }


# ---------------------------------------------------------------------------
# 八、报告：format_contract_report
# ---------------------------------------------------------------------------


def _fmt_time_columns_section(profile: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    cols = profile["time_columns"]
    lines.append("### 时间列（业务时间的唯一可信来源）")
    lines.append("")
    if not cols:
        lines.append("- 未识别到时间列。**注意：没有业务时间列就无法做时间口径审计。**")
        lines.append("")
        return lines
    for c in cols:
        info = c["time"]
        lines.append(
            "- **%s**（%s）：min = `%s`，max = `%s`，跨度 %s 天；解析成功 %d，解析失败 %d，疑似缺失符号 %d（已单列）"
            % (
                c["name"],
                c["position"],
                info["min"],
                info["max"],
                info["span_days"],
                info["parsed"],
                info["failed"],
                info["missing_token_excluded"],
            )
        )
        if info["failed"]:
            lines.append(
                "  - 解析失败样例（**未静默丢弃**）：%s"
                % "、".join("`%s`" % _truncate(v, 20) for v in info["failed_samples"])
            )
        if info["formats"]:
            lines.append(
                "  - 命中的格式：%s"
                % "、".join("`%s`×%d" % (k, v) for k, v in info["formats"].items())
            )
    lines.append("")
    return lines


def _fmt_id_section(profile: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    cols = profile["id_columns"]
    lines.append("### ID 列（一律按字符串处理，不转数字）")
    lines.append("")
    if not cols:
        lines.append("- 未识别到 ID 列。")
        lines.append("")
        return lines
    for c in cols:
        info = c["id"]
        bits = [
            "唯一值 %d / 行数 %d" % (info["unique"], c["n_rows"]),
            "重复值 %d" % info["duplicate_count"],
            "科学计数法 %d" % info["sci_notation_count"],
            "纯数字取值 %d（长度分布 %s）"
            % (
                info["pure_digit_count"],
                "、".join("%s×%d" % (k, v) for k, v in info["digit_lengths"].items()) or "无",
            ),
        ]
        lines.append("- **%s**：%s" % (c["name"], "；".join(bits)))
        if info["sci_notation_count"]:
            lines.append(
                "  - 🔴 **高危：ID 被 Excel 转成科学计数法，精度已丢失**，样例：%s"
                % "、".join("`%s`" % v for v in info["sci_notation_samples"])
            )
        if info["length_inconsistent"]:
            lines.append(
                "  - 🟠 前导零丢失风险：纯数字 ID 长度不一致（可能是 000123 → 123）"
            )
        if info["float_tail_count"]:
            lines.append(
                "  - 🟠 出现 `.0` 结尾的 ID：%s"
                % "、".join("`%s`" % v for v in info["float_tail_samples"])
            )
    lines.append("")
    return lines


def _fmt_money_section(profile: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    cols = profile["money_columns"]
    lines.append("### 金额列（Decimal 合计，禁止 float 累加）")
    lines.append("")
    if not cols:
        lines.append("- 未识别到金额列。")
        lines.append("")
        return lines
    for c in cols:
        info = c["money"]
        lines.append(
            "- **%s**：合计 = `%s`（Decimal）；可解析 %d 个，无法解析 %d 个，疑似缺失符号 %d 个（缺失 ≠ 0，已在合计中排除）"
            % (
                c["name"],
                info["sum"],  # Decimal 累加结果，原样输出不做 float 转换
                info["numeric_count"],
                info["non_numeric_count"],
                info["missing_token_count"],
            )
        )
        lines.append(
            "  - min = `%s`，max = `%s`，负数 %d 个，零值 %d 个"
            % (info["min"], info["max"], info["negative_count"], info["zero_count"])
        )
        if info["non_numeric_count"]:
            lines.append(
                "  - 无法解析样例：%s"
                % "、".join("`%s`" % _truncate(v, 20) for v in info["non_numeric_samples"])
            )
    lines.append("")
    return lines


def _fmt_status_section(profile: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    cols = profile["status_columns"]
    lines.append("### 状态列（取值分布）")
    lines.append("")
    if not cols:
        lines.append("- 未识别到状态列。")
        lines.append("")
        return lines
    for c in cols:
        info = c["status"]
        lines.append("- **%s**：取值 %d 种" % (c["name"], info["n_distinct"]))
        for item in info["top"]:
            lines.append(
                "  - `%s`：%d 行（%.1f%%）"
                % (
                    _truncate(item["value"], 20),
                    item["count"],
                    100.0 * item["count"] / c["n_rows"] if c["n_rows"] else 0.0,
                )
            )
        if info["other_count"]:
            lines.append("  - 其余 %d 种取值未列出（只展示前 10 种）" % info["other_count"])
        if info["missing_token_count"]:
            lines.append("  - 另有 %d 个疑似缺失符号，不要当作某个状态值" % info["missing_token_count"])
    lines.append("")
    return lines


def _fmt_columns_table(profile: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    lines.append("### 列概览")
    lines.append("")
    lines.append("| # | 列名 | 类型推断 | 非空 | 空 | 疑似缺失符号 | 唯一值 | 示例值（最多3个） |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for c in profile["columns"]:
        missing_text = (
            "、".join("%s×%d" % (k, v) for k, v in c["missing_tokens"].items())
            if c["missing_tokens"]
            else "0"
        )
        lines.append(
            "| %d | %s | %s | %d | %d | %s | %d | %s |"
            % (
                c["index"] + 1,
                _md_cell(c["name"], 24),
                c["kind"],
                c["n_nonempty"],
                c["n_empty"],
                _md_cell(missing_text, 24),
                c["n_unique"],
                _md_cell("、".join(c["samples"]), 40) if c["samples"] else "",
            )
        )
    lines.append("")
    lines.append(
        "> 口径说明：**空** = 单元格为空字符串；**疑似缺失符号** = `-`、`—`、`/`、`N/A`、`NA`、`null`、`NULL`、`无`、`未知`、`不适用` 等；"
        "真正的 `0` 既不算空、也不算缺失。三者必须分开统计。"
    )
    lines.append("")
    return lines


def format_contract_report(profiles: List[Dict[str, Any]]) -> str:
    """把 profile_table 的结果渲染成中文 Markdown 报告。"""
    lines: List[str] = []
    lines.append("# 数据契约体检报告")
    lines.append("")
    lines.append(
        "> 本报告由 `scripts/contract.py` **程序生成**：表头、行列数、时间范围、缺失、重复、候选主键、"
        "金额 Decimal 合计全部由程序计算。原则是「关键数字由计算程序产生，模型只负责解释和提假设」。"
    )
    lines.append("> 程序不替你断定业务粒度，所有「一行是什么」的结论都标注了**需人工确认**。")
    lines.append("")

    if not profiles:
        lines.append("（没有可体检的表：请检查文件路径与格式。）")
        return "\n".join(lines)

    lines.append("共体检 **%d** 张表。生成时间：%s" % (len(profiles), datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    lines.append("")

    # 汇总表
    lines.append("## 汇总")
    lines.append("")
    lines.append("| 表 | 行 | 列 | 业务时间范围 | 疑似缺失符号 | 整行重复 | 唯一候选主键 | 一行 = ？ |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for p in profiles:
        missing_total = sum(c["missing_token_total"] for c in p["columns"])
        exact = p["candidate_primary_keys"]["exact"]
        exact_text = "、".join(k["name"] for k in exact[:3]) if exact else "无（见候选比值）"
        top_gran = p["granularity"]["candidates"][0]["granularity"] if p["granularity"]["candidates"] else "未知"
        lines.append(
            "| %s | %d | %d | %s ~ %s | %d | %d | %s | %s |"
            % (
                _md_cell(p["name"], 30),
                p["n_rows"],
                p["n_cols"],
                p.get("data_time_min") or "无时间列",
                p.get("data_time_max") or "无时间列",
                missing_total,
                p["duplicate_rows"]["count"],
                _md_cell(exact_text, 24),
                _md_cell(top_gran + "（需人工确认）", 40),
            )
        )
    lines.append("")

    for order, p in enumerate(profiles, 1):
        sheet_text = p["sheet"] if p["sheet"] else "无（文本文件）"
        lines.append("## %d. %s" % (order, p["name"]))
        lines.append("")
        lines.append("- 绝对路径：`%s`" % p["source"])
        lines.append("- 工作表：%s" % sheet_text)
        lines.append("- **实际行列数：%d 行 × %d 列**（已按真实数据边界裁剪）" % (p["n_rows"], p["n_cols"]))
        lines.append(
            "- 完整表头（共 %d 列）：%s"
            % (p["n_cols"], "、".join("`%s`" % _truncate(h, 24) for h in p["headers"]))
        )
        if p["encoding"]:
            lines.append("- 文本编码：%s" % p["encoding"])
        if p["file_mtime"]:
            lines.append(
                "- 文件修改时间（约等于导出时间，仅供参考，**不是业务发生时间**）：%s" % p["file_mtime"]
            )
        lines.append("")

        if p["load_notes"]:
            lines.append("### 加载说明（读取方式，不是业务结论）")
            lines.append("")
            for note in p["load_notes"]:
                lines.append("- %s" % note)
            lines.append("")

        lines.append("### ⚠️ 风险提示（共 %d 条）" % len(p["warnings"]))
        lines.append("")
        if p["warnings"]:
            for warning in p["warnings"]:
                lines.append("- %s" % warning)
        else:
            lines.append("- 未发现结构性风险。这不代表数据正确，只代表没有明显的格式/口径问题。")
        lines.append("")

        lines.extend(_fmt_columns_table(p))
        lines.extend(_fmt_time_columns_section(p))
        lines.extend(_fmt_id_section(p))
        lines.extend(_fmt_money_section(p))
        lines.extend(_fmt_status_section(p))

        # 候选主键
        lines.append("### 候选主键")
        lines.append("")
        exact = p["candidate_primary_keys"]["exact"]
        if exact:
            lines.append(
                "- 唯一值数 == 行数（可作单列主键）的列：%s"
                % "、".join("**%s**" % k["name"] for k in exact)
            )
        else:
            lines.append("- **没有任何列的「唯一值数 == 行数」**，本表不存在单列主键（常见于汇总表或存在重复行）。")
        lines.append("- 唯一值/行数 比值最高的前三列：")
        for item in p["candidate_primary_keys"]["top_ratio"]:
            lines.append(
                "  - %s：%d/%d = %.4f（%s）"
                % (item["name"], item["unique"], item["rows"], item["ratio"], item["kind"])
            )
        dup = p["duplicate_rows"]
        lines.append(
            "- **整行完全重复的行数：%d**（唯一行数 %d / 总行数 %d）"
            % (dup["count"], dup["unique_rows"], p["n_rows"])
        )
        if dup["samples"]:
            for sample in dup["samples"]:
                lines.append("  - 重复行样例：%s" % _truncate(" | ".join(sample), 90))
        lines.append("")

        # 粒度
        lines.append("### 粒度推断：**一行 = ？**（需人工确认）")
        lines.append("")
        lines.append("**%s**" % p["one_row_is"])
        lines.append("")
        for i, cand in enumerate(p["granularity"]["candidates"], 1):
            lines.append("- 候选 %d：**%s**（置信度：%s）" % (i, cand["granularity"], cand["confidence"]))
            lines.append("  - 理由：%s" % cand["reason"])
            lines.append("  - 影响：%s" % cand["implication"])
        if p["granularity"]["evidence"]:
            lines.append("- 证据：")
            for ev in p["granularity"]["evidence"]:
                lines.append("  - %s" % ev)
        lines.append("")

        # 时间连续性
        temporal = p["temporal"]
        lines.append("### 日期连续性与部分日")
        lines.append("")
        if temporal["date_column"] is None:
            lines.append("- 没有可用的日期列，无法做连续性检查。")
        elif temporal["note"]:
            lines.append("- 用于检查的日期列：%s" % temporal["date_column"])
            lines.append("- %s" % temporal["note"])
            if temporal["rows_per_day_consistent"] is not None:
                lines.append(
                    "- 每天行数是否一致：%s"
                    % ("一致" if temporal["rows_per_day_consistent"] else "**不一致**（说明一行不是「日」）")
                )
        else:
            lines.append("- 用于检查的日期列：%s" % temporal["date_column"])
            lines.append(
                "- 缺失日期：%d 天%s"
                % (
                    temporal["missing_day_count"],
                    ("（%s）" % "、".join(temporal["missing_days"])) if temporal["missing_days"] else "",
                )
            )
            if temporal["rows_per_day_consistent"] is not None:
                lines.append(
                    "- 每天行数是否一致：%s"
                    % ("一致" if temporal["rows_per_day_consistent"] else "**不一致**（说明一行不是「日」）")
                )
            if temporal["partial_last_day"]:
                part = temporal["partial_last_day"]
                lines.append(
                    "- 🔴 **疑似未结束的部分日**：%s 的「%s」合计 %s，其他天中位数 %s，比值 %s（最后一天可能是未完成日，需人工确认）"
                    % (
                        part["date"],
                        part["money_column"],
                        part["last_day_sum"],
                        part["median_other_days"],
                        part["ratio"],
                    )
                )
            else:
                lines.append("- 未发现明显的未结束部分日（仅代表最后一天金额没有异常偏低）。")
        lines.append("")

        # 文件名日期
        check = p["filename_date_check"]
        lines.append("### 文件名日期 vs 数据日期（导出时间 ≠ 业务发生时间）")
        lines.append("")
        lines.append("- 文件名提取到的日期：%s" % (check["filename_date"] or "未提取到"))
        lines.append("- 表内业务时间范围：%s ~ %s" % (check["data_min"] or "无", check["data_max"] or "无"))
        lines.append("- 结论：%s" % check["message"])
        lines.append("")

        # Excel 使用范围
        lines.append("### Excel 使用范围")
        lines.append("")
        lines.append("- %s" % (p["excel_range_warning"] or "未发现空表头列或整列空列。"))
        lines.append("")

        # 可回答 / 不能回答
        lines.append("### 本表可回答 / 不能回答")
        lines.append("")
        lines.append("**可以回答（≤3 条，均由程序算出）：**")
        lines.append("")
        for item in p["qa_can_answer"]:
            lines.append("1. %s" % item)
        lines.append("")
        lines.append("**不能回答（需人工确认）：**")
        lines.append("")
        for item in p["qa_cannot_answer"]:
            lines.append("1. %s" % item)
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append("## 使用提醒")
    lines.append("")
    lines.append("1. 本报告只描述数据形态，**不判断业务对错**；任何跨表结论都必须先确认「一行 = ？」。")
    lines.append("2. 金额只认本报告的 Decimal 合计；不要用 Excel 手点或用 float 复算。")
    lines.append("3. 缺失（空 / `-` / `N/A`）在业务上的含义必须人工确认，**不能默认等于 0**。")
    lines.append("4. 时间一律以表内业务时间列的 min/max 为准，文件名日期、文件修改时间、导出时间都不能代替。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 九、命令行入口
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口：返回退出码（0 成功；1 至少一个文件失败）。"""
    parser = argparse.ArgumentParser(
        prog="contract.py",
        description="抖音来客 / 本地推 数据契约体检：读表头、算行列数、找主键、查缺失与重复（原始文件只读）。",
    )
    parser.add_argument("files", nargs="+", help="要体检的文件：.csv / .tsv / .txt / .xlsx / .xlsm")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON（Decimal 用字符串）")
    parser.add_argument("--sheet", default=None, help="Excel 工作表名（默认取第一个工作表）")
    parser.add_argument("--encoding", default=None, help="强制指定文本编码，如 gbk、utf-8-sig")
    args = parser.parse_args(argv)

    profiles: List[Dict[str, Any]] = []
    failures: List[str] = []
    for path in args.files:
        try:
            table = load_table(path, sheet=args.sheet, encoding=args.encoding)
            profiles.append(profile_table(table))
        except Exception as exc:  # CLI 不应该抛栈给用户看
            message = "%s：%s" % (path, exc)
            failures.append(message)
            print("错误：%s" % message, file=sys.stderr)

    if args.json:
        payload = {"tables": profiles, "failures": failures, "module_version": VERSION}
        sys.stdout.write(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n"
        )
    else:
        sys.stdout.write(format_contract_report(profiles) + "\n")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
