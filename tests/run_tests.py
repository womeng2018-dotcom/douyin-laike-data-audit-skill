#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/run_tests.py —— 单元测试与端到端校验

跑法：
    python3 tests/run_tests.py
    python3 tests/run_tests.py -v

覆盖三件事：
    1. 公式库 metrics.py 的每个算法（含边界：分母为 0、n=1、缺失值）；
    2. 规范自检题的 8 个案例必须全 PASS（selftest.py）；
    3. 端到端：contract.py 体检 → reconcile.py 对账 → cohort.py 成熟批次，
       用 examples/data 下的**模拟**数据跑真实命令。

任何一个测试失败都以非零退出码结束 —— 这样"规则被破坏"会立刻暴露，
而不是悄悄进入一份看起来很完整的报告。
"""

from __future__ import annotations

import csv
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SCRIPTS = os.path.join(_ROOT, "scripts")
for _p in (_SCRIPTS, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import metrics as M  # noqa: E402

try:
    import contract as C  # noqa: E402
except Exception:  # pragma: no cover
    C = None
try:
    import reconcile as R  # noqa: E402
except Exception:  # pragma: no cover
    R = None
try:
    import selftest as S  # noqa: E402
except Exception:  # pragma: no cover
    S = None

DEMO = os.path.join(_ROOT, "examples", "data")


def _run(args, expect_zero=True):
    """在仓库根目录跑一个脚本，返回 (returncode, stdout, stderr)。"""
    proc = subprocess.run([sys.executable] + args, cwd=_ROOT,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    if expect_zero:
        assert proc.returncode == 0, "命令 %s 失败（exit=%d）\nSTDOUT:%s\nSTDERR:%s" % (
            " ".join(args), proc.returncode, out[-2000:], err[-2000:])
    return proc.returncode, out, err


# ==========================================================================
# 1. 公式库
# ==========================================================================

class TestMissingAndDecimal(unittest.TestCase):
    """缺失值绝不能被当成 0。"""

    def test_missing_tokens_are_none_not_zero(self):
        for token in ("", "-", "—", "N/A", "null", "无", "未知", "不适用", "  "):
            self.assertIsNone(M.to_decimal(token), "『%s』必须视为缺失" % token)

    def test_zero_is_zero_not_missing(self):
        self.assertEqual(M.to_decimal("0"), Decimal("0"))
        self.assertEqual(M.to_decimal(0), Decimal("0"))
        self.assertFalse(M.is_missing("0"))
        self.assertFalse(M.is_missing(0))

    def test_money_parsing(self):
        self.assertEqual(M.to_decimal("¥1,234.50"), Decimal("1234.50"))
        self.assertEqual(M.to_decimal("1,234.5元"), Decimal("1234.5"))
        self.assertEqual(M.to_decimal("(123.45)"), Decimal("-123.45"))

    def test_no_float_noise(self):
        # 0.1 + 0.2 用 Decimal 必须是 0.3
        total = M.to_decimal("0.1") + M.to_decimal("0.2")
        self.assertEqual(total, Decimal("0.3"))


class TestRatios(unittest.TestCase):
    """第七节：比例、加权汇总和金额口径。"""

    def test_zero_denominator_is_not_computable_not_zero(self):
        r = M.safe_div(0, 0)
        self.assertFalse(r.ok)
        self.assertIn("不可计算", r.display(as_percent=True))
        self.assertNotIn("0.00%", r.display(as_percent=True))

    def test_pooled_roi_case1(self):
        r = M.pooled_roi([{"spend": 100, "roi": 3}, {"spend": 10000, "roi": 1}])
        self.assertEqual(r.value.quantize(Decimal("0.0001")), Decimal("1.0198"))
        self.assertEqual(r.numerator, Decimal("10300"))
        self.assertEqual(r.denominator, Decimal("10100"))
        self.assertNotEqual(r.value, Decimal("2"))

    def test_pooled_ratio_never_averages_percentages(self):
        # (1/100 + 0/100) 的正确总体比例是 0.005，而不是 0.5
        r = M.pooled_ratio([(1, 100), (0, 100)])
        self.assertEqual(r.value, Decimal("0.005"))

    def test_ratio_of_sums_matches_pooled(self):
        r = M.ratio_of_sums([1, 0], [100, 100])
        self.assertEqual(r.value, Decimal("0.005"))

    def test_refund_ratio_keeps_original_denominator(self):
        r = M.refund_order_ratio(10, 100)
        self.assertEqual(r.value, Decimal("0.1"))
        self.assertIn("不得通过剔除失败订单", r.note)

    def test_attribution_roi_carries_warning(self):
        r = M.attribution_roi(40000, 400, model="7天点击", window="7天")
        self.assertEqual(r.value, Decimal("100"))
        self.assertIn("不等于", r.note.replace(" ≠ ", "不等于"))


class TestChange(unittest.TestCase):
    """第八节：变化量、百分点 vs 百分比。"""

    def test_percentage_point_vs_relative(self):
        self.assertEqual(M.pct_point_delta(0.05, 0.04).value, Decimal("0.01"))
        self.assertEqual(M.relative_change(0.05, 0.04).value, Decimal("0.25"))

    def test_zero_base_has_no_growth_rate(self):
        r = M.relative_change(5, 0)
        self.assertFalse(r.ok)
        self.assertIn("基期为 0", r.reason)

    def test_delta_amount(self):
        self.assertEqual(M.delta_amount(120, 100).value, Decimal("20"))


class TestDistribution(unittest.TestCase):
    """第八节：分布描述。"""

    def test_mean_excludes_missing_and_reports_it(self):
        r = M.mean([10, 20, "-", None, 30])
        self.assertEqual(r.value, Decimal("20"))
        self.assertIn("2", r.note)

    def test_median_linear_interpolation(self):
        # 偶数个样本：线性插值取中间两数平均
        self.assertEqual(M.median([1, 2, 3, 4]).value, Decimal("2.5"))
        self.assertEqual(M.median([1, 2, 3]).value, Decimal("2"))

    def test_quartiles(self):
        vals = list(range(1, 101))
        self.assertEqual(M.quantile_linear(vals, 0.25).value, Decimal("25.75"))
        self.assertEqual(M.quantile_linear(vals, 0.75).value, Decimal("75.25"))

    def test_sample_stdev_needs_two(self):
        self.assertFalse(M.sample_stdev([5]).ok)
        self.assertIn("n = 1", M.sample_stdev([5]).reason)

    def test_sample_stdev_known_value(self):
        v = float(M.sample_stdev([2, 4, 4, 4, 5, 5, 7, 9]).value)
        self.assertAlmostEqual(v, 2.13809, places=4)

    def test_cv_needs_positive_mean(self):
        self.assertFalse(M.cv([-1, -2, -3]).ok)
        self.assertTrue(M.cv([10, 12, 14]).ok)

    def test_iqr_flags_but_never_removes(self):
        vals = [10, 11, 12, 13, 14, 15, 1000]
        res = M.iqr_outliers(vals)
        self.assertIn(6, res["outlier_indexes"])
        self.assertIn("不是删除依据", res["note"])
        self.assertEqual(len(vals), 7)  # 原始数据一个都没动

    def test_iqr_unreliable_for_tiny_samples(self):
        self.assertIsNone(M.iqr_outliers([1, 2])["iqr"])


class TestWilson(unittest.TestCase):
    """第十一节：Wilson 比例区间。"""

    def test_matches_reference_formula(self):
        k, n, z = 30, 100, 1.96
        p = k / n
        denom = 1 + z * z / n
        center = (p + z * z / (2 * n)) / denom
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
        r = M.wilson_interval(k, n)
        self.assertAlmostEqual(r.meta["center"], center, places=12)
        self.assertAlmostEqual(r.meta["half_width"], half, places=12)
        self.assertAlmostEqual(r.meta["lower"], center - half, places=12)

    def test_knows_its_limits(self):
        r = M.wilson_interval(1, 10)
        self.assertIn("不能弥补", r.note)
        self.assertIn("p 值不是原假设为真的概率", r.note)

    def test_rejects_bad_inputs(self):
        self.assertFalse(M.wilson_interval(1, 0).ok)
        self.assertFalse(M.wilson_interval(5, 3).ok)


class TestMaturity(unittest.TestCase):
    """第九节：同批次、同观察年龄。"""

    def setUp(self):
        self.T = datetime(2026, 9, 10, 12, 0, 0)

    def _rows(self):
        return [
            {"订单ID": "A", "支付时间": (self.T - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"),
             "核销时间": (self.T - timedelta(days=5) + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")},
            {"订单ID": "B", "支付时间": (self.T - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S"),
             "核销时间": ""},
            {"订单ID": "C", "支付时间": (self.T - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"),
             "核销时间": ""},
        ]

    def test_only_mature_orders_enter_denominator(self):
        r = M.cohort_rate(self._rows(), t0_key="支付时间", event_key="核销时间",
                          cutoff=self.T, hours=24, dedupe_key="订单ID")
        self.assertEqual(r.value, Decimal("0.5"))
        self.assertEqual(r.meta["n_eligible"], 2)
        self.assertEqual(r.meta["n_immature"], 1)

    def test_mature_cohort_boundary_is_inclusive(self):
        # t0 + H == T 必须算成熟（<= 而不是 <）
        rows = [{"订单ID": "X",
                 "支付时间": (self.T - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")}]
        co = M.mature_cohort(rows, "支付时间", self.T, 24)
        self.assertEqual(co["n_eligible"], 1)

    def test_one_order_many_coupons_hits_if_any_row_hits(self):
        # 一单两券：第一行没核销时间、第二行有 —— 该订单必须算命中
        rows = [
            {"订单ID": "O1", "支付时间": "2026-09-01 10:00:00", "核销时间": ""},
            {"订单ID": "O1", "支付时间": "2026-09-01 10:00:00", "核销时间": "2026-09-01 18:00:00"},
            {"订单ID": "O2", "支付时间": "2026-09-01 10:00:00", "核销时间": ""},
        ]
        r = M.cohort_rate(rows, t0_key="支付时间", event_key="核销时间",
                          cutoff=self.T, hours=24, dedupe_key="订单ID")
        self.assertEqual(r.value, Decimal("0.5"))  # 2 单里命中 1 单

    def test_unparsable_dates_are_counted_not_dropped(self):
        rows = self._rows() + [{"订单ID": "D", "支付时间": "不是日期", "核销时间": ""}]
        r = M.cohort_rate(rows, t0_key="支付时间", event_key="核销时间",
                          cutoff=self.T, hours=24, dedupe_key="订单ID")
        self.assertEqual(r.meta["n_unparsable"], 1)
        self.assertEqual(r.meta["n_eligible"], 2)

    def test_curve_uses_one_cohort(self):
        rows = self._rows() + [
            {"订单ID": "D", "支付时间": "2026-08-01 10:00:00",
             "核销时间": "2026-08-03 10:00:00"}]
        curve = M.cohort_curve(rows, t0_key="支付时间", event_time_key="核销时间",
                               cutoff=self.T, hours_list=[24, 72], dedupe_key="订单ID")
        self.assertEqual(len(curve), 2)
        self.assertEqual(curve[0].meta["n_cohort"], curve[1].meta["n_cohort"])
        self.assertEqual(curve[0].meta["n_hit"], 1)   # 只有 A 在 24h 内
        self.assertEqual(curve[1].meta["n_hit"], 2)   # A 和 D 都在 72h 内

    def test_cohort_curve_requires_cutoff(self):
        with self.assertRaises(ValueError):
            M.mature_cohort([], "支付时间", None, 24)


class TestDedupeAndStandardize(unittest.TestCase):
    """第五、十节。"""

    def test_dedupe_count_one_order_two_coupons(self):
        rows = [{"订单ID": "O1", "券码": "C1"}, {"订单ID": "O1", "券码": "C2"}]
        self.assertEqual(M.dedupe_count(rows, "订单ID")["distinct"], 1)
        self.assertEqual(M.dedupe_count(rows, "券码")["distinct"], 2)

    def test_dedupe_reports_missing_keys(self):
        res = M.dedupe_count([{"订单ID": "O1"}, {"订单ID": ""}], "订单ID")
        self.assertEqual(res["missing_key_rows"], 1)

    def test_standardize_requires_weights_sum_one(self):
        groups = [{"name": "A", "numerator": 1, "denominator": 10},
                  {"name": "B", "numerator": 5, "denominator": 10}]
        self.assertFalse(M.standardize_ratio(groups, [0.5, 0.6]).ok)

    def test_standardize_computes_weighted_rate(self):
        groups = [{"name": "A", "numerator": 1, "denominator": 10},
                  {"name": "B", "numerator": 5, "denominator": 10}]
        r = M.standardize_ratio(groups, [0.5, 0.5])
        self.assertEqual(r.value, Decimal("0.3"))

    def test_standardize_does_not_silently_drop_missing_groups(self):
        groups = [{"name": "A", "numerator": 1, "denominator": 10},
                  {"name": "B", "numerator": 0, "denominator": 0}]
        r = M.standardize_ratio(groups, [0.5, 0.5])
        self.assertIn("B", r.note)


class TestGuards(unittest.TestCase):
    """第七、十一、十二节：护栏函数。"""

    def test_evidence_labels_are_the_four_allowed(self):
        for k in ("reconciled", "observational", "hypothesis", "insufficient"):
            self.assertIn(M.evidence_label(k).value, M.EVIDENCE_LABELS)
        self.assertFalse(M.evidence_label("87%可信").ok)

    def test_causal_guard_refuses_total_sales_over_spend(self):
        g = M.causal_increment_guard()
        self.assertEqual(g["evidence_label"], "数据不足")
        g2 = M.causal_increment_guard(has_attribution=True)
        self.assertEqual(g2["evidence_label"], "待验证假设")
        g3 = M.causal_increment_guard(has_attribution=True, has_control_or_preperiod=True)
        self.assertEqual(g3["evidence_label"], "观察性差异")
        g4 = M.causal_increment_guard(has_attribution=True, has_control_or_preperiod=True,
                                      has_random_assignment=True)
        self.assertEqual(g4["evidence_label"], "已对账事实")

    def test_net_revenue_guard_requires_reconciliation_and_costs(self):
        self.assertEqual(M.net_revenue_guard()["evidence_label"], "数据不足")
        self.assertFalse(M.net_revenue_guard(has_reconciliation=True)["can_profit"])
        self.assertTrue(M.net_revenue_guard(
            has_reconciliation=True, has_fee_and_subsidy_policy=True,
            has_fulfillment_cost=True)["can_profit"])

    def test_independent_units_guard_3000_orders_2_days(self):
        g = M.independent_units_guard(3000, 2)
        self.assertEqual(g["n_independent_units"], 2)
        self.assertFalse(g["ok_to_infer"])
        self.assertFalse(g["record_level_n_usable"])
        self.assertIn("固定门槛", g["note"])

    def test_multiple_comparison_guard(self):
        self.assertIn("探索性", M.multiple_comparison_guard(57)["verdict"])


# ==========================================================================
# 2. 规范自检题必须全 PASS
# ==========================================================================

@unittest.skipIf(S is None, "selftest.py 不可导入")
class TestSpecSelftest(unittest.TestCase):
    def test_all_eight_cases_pass(self):
        cases = S.run_all()
        self.assertEqual(len(cases), 8)
        failed = ["案例%d %s" % (c.id, c.title) for c in cases if not c.passed]
        self.assertEqual(failed, [], "以下自检案例未通过：%s" % failed)

    def test_case2_reverse_validation_catches_naive_join(self):
        """案例 2 必须真的证明错误做法会被拦下，而不只是宣称。"""
        if R is None:
            self.skipTest("reconcile.py 不可用")
        cases = {c.id: c for c in S.run_all()}
        descs = [chk.desc for chk in cases[2].checks]
        self.assertTrue(any("失败" in d for d in descs),
                        "案例 2 缺少『直接 join 被判失败』的反向验证")


# ==========================================================================
# 3. 端到端（用 examples/data 的模拟数据）
# ==========================================================================

class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.isdir(DEMO) or not os.listdir(DEMO):
            raise unittest.SkipTest("缺少 examples/data，先运行 examples/make_demo.py")

    def test_selftest_cli_exit_zero(self):
        rc, out, _ = _run(["scripts/selftest.py", "--quiet"])
        self.assertEqual(rc, 0)
        self.assertNotIn("FAIL", out)

    def test_contract_cli_runs_on_demo(self):
        rc, out, _ = _run(["scripts/contract.py",
                           "examples/data/demo_orders.csv",
                           "examples/data/demo_coupons.csv"])
        self.assertIn("数据契约体检报告", out)
        self.assertIn("一行", out)          # 必须回答「一行是什么」
        self.assertIn("券码", out)

    def test_contract_cli_json_is_valid(self):
        rc, out, _ = _run(["scripts/contract.py", "examples/data/demo_orders.csv", "--json"])
        data = json.loads(out)
        self.assertTrue(data)

    def test_contract_detects_missing_symbol_not_zero(self):
        if C is None:
            self.skipTest("contract.py 不可用")
        t = C.load_table(os.path.join(DEMO, "demo_refunds.csv"))
        prof = C.profile_table(t)
        blob = json.dumps(prof, ensure_ascii=False, default=str)
        self.assertIn("缺失", blob)

    def test_cohort_cli_on_demo_orders(self):
        rc, out, _ = _run([
            "scripts/cohort.py", "--file", "examples/data/demo_orders.csv",
            "--t0", "支付时间", "--event", "订单状态",
            "--cutoff", "2024-09-01 00:00:00", "--hours", "24",
            "--dedupe", "订单ID"])
        self.assertIn("成熟条件", out)
        self.assertIn("分母", out)

    def test_cohort_cli_rejects_unknown_column(self):
        rc, out, err = _run([
            "scripts/cohort.py", "--file", "examples/data/demo_orders.csv",
            "--t0", "不存在的列", "--event", "订单状态",
            "--cutoff", "2024-09-01 00:00:00"], expect_zero=False)
        self.assertEqual(rc, 2)
        self.assertIn("找不到列", err)

    def test_report_scaffold_contains_all_sections(self):
        rc, out, _ = _run(["scripts/report.py", "--scaffold"])
        for section in ("核心结论", "数据范围与口径", "关键数字", "不能下的结论",
                        "下一步", "统计学教学", "发布前 12 项核对"):
            self.assertIn(section, out)

    def test_report_injects_facts(self):
        import tempfile
        facts = [{"label": "总体ROI", "value": "1.0198", "numerator": "10300",
                  "denominator": "10100", "formula": "Σ成交/Σ消耗",
                  "source": "demo.csv", "note": "归因口径未说明"}]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as fh:
            json.dump(facts, fh, ensure_ascii=False)
            path = fh.name
        try:
            rc, out, _ = _run(["scripts/report.py", "--facts", path])
            self.assertIn("10300", out)
            self.assertIn("Σ成交/Σ消耗", out)
            self.assertIn("归因口径未说明", out)
        finally:
            os.unlink(path)

    @unittest.skipIf(R is None, "reconcile.py 不可用")
    def test_reconcile_cli_join_detects_amplification(self):
        import tempfile
        d = tempfile.mkdtemp()
        left = os.path.join(d, "orders.csv")
        right = os.path.join(d, "coupons.csv")
        with open(left, "w", encoding="utf-8-sig") as fh:
            fh.write("订单ID,订单实收金额\nO1,200\n")
        with open(right, "w", encoding="utf-8-sig") as fh:
            fh.write("券码,订单ID,核销金额,购买数量\nC1,O1,100,2\nC2,O1,100,2\n")
        rc, out, err = _run(["scripts/reconcile.py", "join", "--left", left,
                             "--right", right, "--left-key", "订单ID",
                             "--right-key", "订单ID", "--left-values", "订单实收金额",
                             "--right-values", "核销金额"], expect_zero=False)
        blob = out + err
        self.assertIn("订单ID", blob)
        self.assertTrue(("失败" in blob) or ("放大" in blob),
                        "直接 join 必须报告金额放大/失败，实际输出：%s" % blob[-800:])
        self.assertEqual(rc, 2, "金额放大时退出码必须是 2（失败）")

    @unittest.skipIf(R is None, "reconcile.py 不可用")
    def test_reconcile_to_decimal_missing_is_none(self):
        self.assertIsNone(R.to_decimal("-"))
        self.assertEqual(R.to_decimal("¥1,234.50"), Decimal("1234.5"))


class TestSafetyNets(unittest.TestCase):
    """护栏测试：这些是"看似完整但其实错"的最后一道防线。"""

    def test_cohort_rate_refuses_when_event_time_column_is_a_status(self):
        """把状态列误当时间列时，必须报"不可计算"，绝不能输出 0%。"""
        rows = [{"订单ID": "A", "支付时间": "2024-08-01 09:00:00", "订单状态": "已完成"},
                {"订单ID": "B", "支付时间": "2024-08-02 09:00:00", "订单状态": "已完成"}]
        r = M.cohort_rate(rows, t0_key="支付时间", event_key="订单状态",
                          cutoff="2024-09-01", hours=24, event_time_key="订单状态",
                          dedupe_key="订单ID")
        self.assertFalse(r.ok, "必须拒绝输出，而不是给出 0%")
        self.assertIn("没有一个能按时间格式解析", r.reason)
        self.assertIn("拒绝输出 0%", r.note)

    def test_cohort_rate_presence_semantics_when_no_event_time(self):
        """不传 event_time_key 时，用"有值即命中"语义。"""
        rows = [{"订单ID": "A", "支付时间": "2024-08-01 09:00:00", "订单状态": "已完成"},
                {"订单ID": "B", "支付时间": "2024-08-02 09:00:00", "订单状态": ""}]
        r = M.cohort_rate(rows, t0_key="支付时间", event_key="订单状态",
                          cutoff="2024-09-01", hours=24, dedupe_key="订单ID")
        self.assertEqual(r.value, Decimal("0.5"))

    def test_looks_like_time_column(self):
        rows = [{"t": "2024-08-01 09:00:00"}, {"t": "2024-08-02 09:00:00"},
                {"t": "已完成"}, {"t": "已完成"}]
        ok, parsed, non_missing, _ = M.looks_like_time_column(rows, "t")
        self.assertFalse(ok)
        self.assertEqual(parsed, 2)
        self.assertEqual(non_missing, 4)
        rows2 = [{"t": "2024-08-01 09:00:00"}, {"t": "2024-08-02 09:00:00"}]
        self.assertTrue(M.looks_like_time_column(rows2, "t")[0])

    def test_report_renders_percent_not_raw_decimal(self):
        import report as RP
        line = RP._fmt_num({"label": "核销比例", "value": "0.1578947368421052631",
                            "numerator": "3", "denominator": "19",
                            "as_percent": True, "source": "x.csv"})
        self.assertIn("15.79%", line)
        self.assertNotIn("0.1578947368421052631", line)

    def test_report_rounds_long_decimals(self):
        import report as RP
        line = RP._fmt_num({"label": "ROI", "value": "1.0198019801980198"})
        self.assertIn("1.0198", line)
        self.assertNotIn("1.0198019801980198", line)

    def test_report_marks_not_computable(self):
        import report as RP
        line = RP._fmt_num({"label": "退款率", "computable": False,
                            "reason": "分母为 0"})
        self.assertIn("不可计算", line)
        self.assertNotIn("0.00%", line)


class TestCohortPipeline(unittest.TestCase):
    """端到端：先聚合再关联 → 成熟批次表，对象表行数不得放大。"""

    @classmethod
    def setUpClass(cls):
        if not os.path.isdir(DEMO) or not os.listdir(DEMO):
            raise unittest.SkipTest("缺少 examples/data")
        cls.tmp = tempfile.mkdtemp()

    def test_build_cohort_table_does_not_inflate_rows(self):
        out = os.path.join(self.tmp, "cohort.csv")
        audit = os.path.join(self.tmp, "audit.json")
        rc, stdout, _ = _run([
            "scripts/build_cohort_table.py",
            "--orders", "examples/data/demo_orders.csv",
            "--events", "examples/data/demo_coupons.csv",
            "--key", "订单ID", "--event-time", "核销时间",
            "--out-col", "首次核销时间", "--out", out, "--json-audit", audit])
        with open(out, encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        n_orders = sum(1 for _ in open("examples/data/demo_orders.csv",
                                       encoding="utf-8-sig")) - 1
        self.assertEqual(len(rows), n_orders, "对象表行数不得因关联而放大")
        data = json.load(open(audit, encoding="utf-8"))
        self.assertEqual(data["orders_rows"], n_orders)
        self.assertGreater(data["objects_with_multiple_event_rows"], 0,
                           "演示数据里应当有一单多券，否则这个测试没意义")
        # 未匹配必须留空，不能填 0
        blanks = [r for r in rows if r["首次核销时间"] == ""]
        self.assertTrue(blanks)
        self.assertTrue(all(r["首次核销时间"] != "0" for r in rows))

    def test_cohort_cli_on_built_table(self):
        out = os.path.join(self.tmp, "cohort2.csv")
        _run(["scripts/build_cohort_table.py",
              "--orders", "examples/data/demo_orders.csv",
              "--events", "examples/data/demo_coupons.csv",
              "--key", "订单ID", "--event-time", "核销时间",
              "--out-col", "首次核销时间", "--out", out])
        rc, stdout, _ = _run(["scripts/cohort.py", "--file", out, "--t0", "支付时间",
                              "--event", "首次核销时间", "--cutoff", "2024-08-25 00:00:00",
                              "--hours", "24", "--dedupe", "订单ID",
                              "--curve", "24,72,168"])
        self.assertIn("时间列（自动识别）", stdout)
        self.assertIn("成熟条件", stdout)
        self.assertIn("同一成熟批次的观察节点曲线", stdout)

    def test_make_facts_marks_ratio_as_percent(self):
        rate_json = os.path.join(self.tmp, "rate.json")
        with open(rate_json, "w", encoding="utf-8") as fh:
            json.dump({"rate": {"value": "0.5", "numerator": "1", "denominator": "2",
                                "unit": "", "computable": True,
                                "meta": {"n_hit": 1, "n_eligible": 2}}}, fh)
        facts = os.path.join(self.tmp, "facts.json")
        _run(["scripts/make_facts.py", "--rate-json", rate_json,
              "--label", "24小时核销比例", "--source", "test", "--out", facts])
        data = json.load(open(facts, encoding="utf-8"))
        self.assertTrue(data["facts"][0]["as_percent"])

    def test_cohort_curve_without_time_column_is_refused(self):
        rc, out, err = _run(["scripts/cohort.py",
                             "--file", "examples/data/demo_orders.csv",
                             "--t0", "支付时间", "--event", "订单状态",
                             "--cutoff", "2024-09-01 00:00:00",
                             "--curve", "24,72"], expect_zero=False)
        self.assertEqual(rc, 2)
        self.assertIn("--event-time", err)


if __name__ == "__main__":
    unittest.main(verbosity=2, argv=[sys.argv[0]] + [a for a in sys.argv[1:]
                                                     if not a.startswith("--strict")])
