#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「抖音来客 / 本地推 经营数据统计审计」的**模拟**样例数据。

重要声明
--------
本脚本生成的全部数据都是**模拟数据（合成数据）**，不是任何真实抖音来客 / 本地推后台的导出结果，
数字、门店、商品、订单号都是编造的，不指向任何真实门店或账户。
每一张生成的 CSV 里都显式带有"模拟"字样（门店名统一为 `模拟-申北路店` 等）。

用途
----
给 `scripts/contract.py`（数据契约体检）提供可复现的输入，用来演示这些陷阱：
  1. 一单多券：同一订单两行券，每行"购买数量"都写 2 → 不能把每行购买数量相加；
  2. 缺失 ≠ 0：用 `-` 表示"无退款"，用 `0.00` 表示"确实退了 0 元"，两者必须分开；
  3. 时间列里混入"未支付"文本 → 时间解析失败必须保留样例，不能静默丢弃；
  4. 商品ID 长度不一致且带前导零 → 前导零丢失风险；
  5. 券码里有一个被 Excel 转成科学计数法的值 → ID 精度丢失高危；
  6. 整行完全重复一行 → 直接求和会翻倍；
  7. 日期序列缺一天 + 最后一天是未结束的部分日。

可重复运行：每次运行都会覆盖生成 `data/` 下的 4 个 CSV。

用法
----
    python3 examples/make_demo.py
"""

from __future__ import annotations

import csv
import datetime
import decimal
import os
import random
from typing import List, Optional, Sequence

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

MOCK_TAG = "模拟"
STORES = ["模拟-申北路店", "模拟-莘庄店", "模拟-七宝店"]

# (商品ID, 商品名称, 单价) —— 故意让 ID 长度不一致且带前导零
PRODUCTS = [
    ("0001234", "模拟-双人套餐A", "168.00"),
    ("0002345", "模拟-单人套餐B", "88.00"),
    ("0003456", "模拟-四人套餐C", "328.00"),
    ("12345", "模拟-50元代金券", "50.00"),
]

ORDER_STATUS = ["已完成", "已完成", "已完成", "已完成", "已完成", "待支付", "已取消", "已退款"]


def _d(value) -> decimal.Decimal:
    """把字符串/数字统一转成 Decimal（金额计算绝不用 float）。"""
    return decimal.Decimal(str(value))


def _money(value) -> str:
    """输出两位小数的金额字符串。"""
    return str(_d(value).quantize(decimal.Decimal("0.01")))


def write_csv(path: str, headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    """以 UTF-8-SIG 写出 CSV（不加任何注释行，注释行会破坏解析）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)
    print("已生成 %s（%d 行 × %d 列）" % (os.path.relpath(path, BASE_DIR), len(rows), len(headers)))


def build_orders(rng: random.Random):
    """订单表：21 行正常数据 + 1 行整行重复。"""
    rows: List[List[str]] = []
    order_ids: List[str] = []
    start = datetime.datetime(2024, 8, 1, 9, 0, 0)
    for i in range(21):
        order_id = "D%s%03d" % ((start + datetime.timedelta(days=i % 20)).strftime("%Y%m%d"), i + 1)
        order_ids.append(order_id)
        created = start + datetime.timedelta(days=i % 20, minutes=i * 37 % 600, seconds=i * 13 % 60)
        product_id, product_name, price = rng.choice(PRODUCTS)
        quantity = rng.choice([1, 1, 1, 2, 2, 4])
        paid = _d(price) * quantity
        if i % 7 == 3:  # 少量折扣，让金额不是单价的整数倍
            paid = (paid * _d("0.90")).quantize(decimal.Decimal("0.01"))

        # 陷阱 A：待支付订单的"支付时间"是空的（空值）
        if i == 4:
            pay_time, status, received = "", "待支付", "0.00"
        # 陷阱 B：已取消订单的"支付时间"写的是文本"未支付"（时间解析会失败）
        elif i == 9:
            pay_time, status, received = "未支付", "已取消", "0.00"
        # 陷阱 C：已退款订单的"用户实付金额"用 - 表示没有实付（缺失 ≠ 0）
        elif i == 14:
            pay_time = (created + datetime.timedelta(minutes=6)).strftime("%Y-%m-%d %H:%M:%S")
            status, received = "已退款", "0.00"
            rows.append(
                [
                    order_id,
                    created.strftime("%Y-%m-%d %H:%M:%S"),
                    pay_time,
                    rng.choice(STORES),
                    product_id,
                    product_name,
                    str(quantity),
                    "-",
                    received,
                    status,
                ]
            )
            continue
        else:
            pay_time = (created + datetime.timedelta(minutes=rng.randint(1, 30))).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            status = rng.choice(ORDER_STATUS[:5])
            received = _money(paid)

        rows.append(
            [
                order_id,
                created.strftime("%Y-%m-%d %H:%M:%S"),
                pay_time,
                rng.choice(STORES),
                product_id,
                product_name,
                str(quantity),
                _money(paid),
                received,
                status,
            ]
        )

    # 陷阱 D：整行完全重复（重复导出），直接求和会翻倍
    rows.append(list(rows[2]))
    return rows, order_ids


def build_coupons(order_ids: Sequence[str], rng: random.Random):
    """券表：故意让某些订单"一单多券"，且每行购买数量都写订单级数量。"""
    headers = ["券码", "订单ID", "核销时间", "核销金额", "购买数量", "门店"]
    rows: List[List[str]] = []

    def add(seq: int, order_id: str, verify: str, amount: str, quantity: str, store: str,
            code: Optional[str] = None) -> None:
        coupon_code = code if code else "700123456789%04d" % seq
        rows.append([coupon_code, order_id, verify, amount, quantity, store])

    # 一单多券：D20240803003 有 2 张券，两行"购买数量"都写 2（真实订单数量是 2）
    multi_order_1 = order_ids[2]
    add(1, multi_order_1, "2024-08-03 12:11:05", "34.00", "2", STORES[0])
    add(2, multi_order_1, "2024-08-03 12:11:40", "34.00", "2", STORES[0])
    # 一单多券：另一单 2 张券，购买数量都为 1
    multi_order_2 = order_ids[7]
    add(3, multi_order_2, "2024-08-08 19:02:10", "44.00", "1", STORES[1])
    add(4, multi_order_2, "2024-08-08 19:05:00", "44.00", "1", STORES[1])

    # 其余单券订单（其中一张券的券码用来演示"被 Excel 转成科学计数法"）
    seq = 10
    for i in range(10):
        order_id = order_ids[i]
        if order_id in (multi_order_1, multi_order_2):
            continue
        day = 1 + (i * 2) % 18
        verify = "2024-08-%02d %02d:%02d:00" % (day, 10 + i % 9, (i * 7) % 60)
        amount = _money(_d(rng.choice(["34.00", "44.00", "84.00", "25.00"])))
        # 陷阱 E：这张券码的精度已经被 Excel 吃掉，无法还原
        code = "7.00123E+15" if seq == 13 else None
        add(seq, order_id, verify, amount, str(rng.choice([1, 1, 2])), rng.choice(STORES), code=code)
        seq += 1

    # 陷阱 F：未核销的券，核销时间与核销金额都用 - 表示"没有发生"
    add(30, order_ids[12], "-", "-", "1", STORES[2])
    return headers, rows


def build_refunds(order_ids: Sequence[str]):
    """退款表：含"无退款 -> 用 - 表示"和"确实退了 0 元 -> 0.00"两种，必须分开看。"""
    headers = ["退款单号", "订单ID", "退款时间", "退款金额", "退款状态", "门店"]
    rows = [
        ["R20240802001", order_ids[14], "2024-08-03 09:15:00", "168.00", "退款成功", STORES[0]],
        ["R20240805002", order_ids[5], "2024-08-05 20:40:00", "88.00", "退款成功", STORES[1]],
        # 陷阱 G：没有退款发生 → 用 - 表示（缺失 ≠ 0）
        ["R20240807003", order_ids[8], "-", "-", "无退款", STORES[0]],
        # 对照：确实退了 0 元 → 这是真实取值 0，不是缺失
        ["R20240808004", order_ids[9], "2024-08-08 10:05:00", "0.00", "已取消退款", STORES[2]],
        ["R20240810005", order_ids[16], "2024-08-10 15:32:00", "50.00", "部分退款", STORES[0]],
        ["R20240812006", order_ids[3], "2024-08-12 21:00:00", "328.00", "退款成功", STORES[1]],
        ["R20240814007", order_ids[18], "2024-08-14 08:45:00", "34.00", "退款处理中", STORES[2]],
        ["R20240816008", order_ids[11], "2024-08-16 17:10:00", "88.00", "退款成功", STORES[1]],
    ]
    return headers, rows


def build_daily():
    """按天汇总表：故意缺一天（2024-08-07），最后一天是未结束的部分日。"""
    headers = ["日期", "门店", "消耗", "成交金额", "订单数"]
    rows: List[List[str]] = []
    days = [d for d in range(1, 15) if d != 7]  # 缺 2024-08-07
    for day in days:
        date_text = "2024-08-%02d" % day
        # 陷阱 H：最后一天（08-14）是导出时尚未结束的部分日，数据明显偏低
        if day == 14:
            spend, gmv, orders = "12.00", "35.00", "1"
        else:
            spend = _money(_d(260 + day * 7))
            gmv = _money(_d(1560 + day * 42))
            orders = str(18 + day)
        rows.append([date_text, STORES[0], spend, gmv, orders])
    return headers, rows


def main() -> int:
    rng = random.Random(20240801)  # 固定种子：每次运行生成完全一样的数据
    os.makedirs(DATA_DIR, exist_ok=True)

    print("正在生成【模拟数据】（非真实后台导出）→ %s" % os.path.relpath(DATA_DIR, BASE_DIR))
    print("所有门店名均带「%s」字样；这些数字是编造的，不可用于任何真实经营结论。" % MOCK_TAG)
    print("-" * 72)

    order_rows, order_ids = build_orders(rng)
    write_csv(
        os.path.join(DATA_DIR, "demo_orders.csv"),
        ["订单ID", "下单时间", "支付时间", "门店", "商品ID", "商品名称", "购买数量", "用户实付金额", "订单实收金额", "订单状态"],
        order_rows,
    )

    coupon_headers, coupon_rows = build_coupons(order_ids, rng)
    write_csv(os.path.join(DATA_DIR, "demo_coupons.csv"), coupon_headers, coupon_rows)

    refund_headers, refund_rows = build_refunds(order_ids)
    write_csv(os.path.join(DATA_DIR, "demo_refunds.csv"), refund_headers, refund_rows)

    daily_headers, daily_rows = build_daily()
    write_csv(os.path.join(DATA_DIR, "demo_daily.csv"), daily_headers, daily_rows)

    print("-" * 72)
    print("完成。每个文件都含「模拟」字样；表结构陷阱说明见 examples/README.md。")
    print("下一步自测：python3 scripts/contract.py examples/data/demo_orders.csv examples/data/demo_coupons.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
