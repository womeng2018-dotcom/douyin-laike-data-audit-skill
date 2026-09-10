#!/usr/bin/env bash
# 端到端演示：用 examples/data 下的【模拟数据】走一遍完整流程
#
# 因为脚本用了 set -e，任何一步失败都会立刻中断——
# 这正是我们要的：宁可中断，也不产出一份看起来完整但算错的报告。
#
# 跑法：bash examples/end_to_end_demo.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DATA="examples/data"
OUT="examples/out"
CUTOFF="2024-08-25 00:00:00"   # 可靠的数据观察截止时间 T（必须由人明确给出）
mkdir -p "$OUT"

hr() { printf '\n\033[1m%s\033[0m\n' "============================================================"; printf '\033[1m%s\033[0m\n' "$1"; printf '\033[1m%s\033[0m\n' "============================================================"; }

hr "0. 生成模拟数据（每次运行都会覆盖，结果可复现）"
python3 examples/make_demo.py

hr "1. 开工前自检 —— 8 个规范案例，必须全 PASS"
python3 scripts/selftest.py --quiet

hr "2. 数据契约体检 —— 每张表『一行是什么』、时间范围、主键、缺失、重复"
python3 scripts/contract.py "$DATA/demo_orders.csv" "$DATA/demo_coupons.csv" \
    > "$OUT/01_数据契约.md"
echo "已写入 $OUT/01_数据契约.md"
grep -E '^\| demo_' "$OUT/01_数据契约.md" || true
echo "--- 风险提示（节选）---"
grep -E '^- .*【高危】|^- .*缺失|^- .*重复|^- .*不连续' "$OUT/01_数据契约.md" | head -8 || true

hr "3. 关联审计 —— 订单表 ⋈ 核销表（一单多券，必须报出金额放大）"
set +e
python3 scripts/reconcile.py join \
    --left "$DATA/demo_orders.csv" --right "$DATA/demo_coupons.csv" \
    --left-key 订单ID --right-key 订单ID \
    --left-values 订单实收金额 --right-values 核销金额 \
    > "$OUT/02_关联审计.md" 2>&1
JOIN_RC=$?
set -e
echo "退出码 = $JOIN_RC （2 = 判定『失败-禁止使用该结果』，正是期望结果）"
grep -iE '放大|失败|一对一|一对多|多对一|多对多' "$OUT/02_关联审计.md" | head -10 || true

hr "4. 分组回加对账 —— 按门店汇总 vs 总量"
python3 scripts/reconcile.py group --file "$DATA/demo_daily.csv" \
    --group 门店 --values 消耗 成交金额 > "$OUT/03_分组回加.md" 2>&1 || true
grep -iE '合计|差|一致|不一致' "$OUT/03_分组回加.md" | head -8 || true

hr "5. 构建订单级成熟批次表（先按订单聚合核销时间，再 1:1 关联）"
# 这一步本身就是规范第五节的正确做法示范：
#   核销表一单多券 → 先按 订单ID 聚合出「首次核销时间」，再与订单表 1:1 关联。
#   直接 join 会让订单金额翻倍（第 3 步已经证明了这一点，所以这里必须换一条路径）。
python3 scripts/build_cohort_table.py \
    --orders "$DATA/demo_orders.csv" --events "$DATA/demo_coupons.csv" \
    --key 订单ID --event-time 核销时间 --out-col 首次核销时间 \
    --out "$OUT/order_cohort.csv"

hr "5b. 同批次同观察年龄的核销率（成熟条件由程序把关）"
python3 scripts/cohort.py \
    --file "$OUT/order_cohort.csv" --t0 支付时间 \
    --event 首次核销时间 --cutoff "$CUTOFF" \
    --hours 24 --dedupe 订单ID --curve 24,72,168 \
    > "$OUT/04_成熟批次.md"
sed -n '1,50p' "$OUT/04_成熟批次.md"

hr "6. 报告骨架 + 事实注入（关键数字不手抄）"
python3 scripts/report.py --scaffold > "$OUT/05_报告骨架.md"
python3 scripts/cohort.py --file "$OUT/order_cohort.csv" --t0 支付时间 \
    --event 首次核销时间 --cutoff "$CUTOFF" --hours 24 \
    --dedupe 订单ID --json > "$OUT/04_成熟批次.json"

python3 scripts/make_facts.py \
    --rate-json "$OUT/04_成熟批次.json" \
    --label "24小时窗口内核销订单比例（模拟数据）" \
    --source "examples/data/demo_orders.csv + demo_coupons.csv（先按订单聚合再关联）" \
    --out "$OUT/facts.json"

python3 scripts/report.py --facts "$OUT/facts.json" --out "$OUT/06_报告.md"
sed -n '/^## 3\. 关键数字/,/^## 4\./p' "$OUT/06_报告.md" | head -10

hr "7. 全部测试（公式边界 + 8 个自检案例 + 端到端）"
python3 tests/run_tests.py 2>&1 | tail -4

hr "完成"
echo "产物在 $OUT/ 下："
ls -1 "$OUT/"
echo
echo "注意：以上全部基于【模拟数据】，不得用于任何真实经营结论。"
