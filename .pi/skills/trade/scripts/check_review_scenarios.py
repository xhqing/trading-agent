#!/usr/bin/env python3
"""复盘报告「金额口径四情景」校验（2026-11-18 用户立）。

为什么需要工具：用户要求「复盘四种口径都要计算，并详细解释，每次详细写在复盘文档里面」。
散文规定会在上下文压缩后衰减（2026-08-18 教训），故做机械校验：报告缺任一情景即判失败。

四情景定义（`review.py` 的 `money_scenarios()` 产出，`--md-block` 直接输出可粘贴的块）：
  ① 下限情景 = mean(R) × 样本内最小 max_loss
  ② 实测金额 = mean(P)（各笔用自己的 max_loss，账户真值）
  ③ 平均风险 = mean(R) × 样本平均 max_loss
  ④ 上限情景 = mean(R) × B（B = risk_fraction × 纸面权益）

用法：
  python3 check_review_scenarios.py reviews/2026-11-14-review-us.md
  python3 check_review_scenarios.py --all reviews/
  python3 check_review_scenarios.py <报告> --min-section 1   # 只要求出现章节（默认要求四项齐全）

退出码：0 通过；1 有缺失。
"""
import argparse
import glob
import os
import re
import sys

# 必须出现的情景名（顺序即报告建议顺序）
SCENARIOS = ["下限情景", "实测金额", "平均风险", "上限情景"]
DIAGS = ["买力折损率", "corr(R, M)", "分层"]
HEAD_PAT = re.compile(r"金额口径四情景|四种金额口径|金额口径.{0,4}情景")
# 样本分布变更点（2026-11-23 立，T209HKUS）：新报告必须亮出 change tag 提醒
TAG_PAT = re.compile(r"样本分布变更点|change tag")
TAG_CUTOFF = "2026-11-23"      # 该日及以后的报告必须有；更早的报告不追溯
DATE_PAT = re.compile(r"(20\d{2}-\d{2}-\d{2})")


def check(path, require_diag=True):
    """返回 (ok, 缺失项列表)。"""
    try:
        txt = open(path, encoding="utf-8").read()
    except FileNotFoundError:
        return False, [f"文件不存在：{path}"]
    missing = []
    if not HEAD_PAT.search(txt):
        missing.append("章节标题（含「金额口径四情景」）")
    for s in SCENARIOS:
        if s not in txt:
            missing.append(f"情景「{s}」")
    if require_diag:
        for d in DIAGS:
            if d not in txt:
                missing.append(f"诊断「{d}」")
    # 变更点提醒：只对 2026-11-23 及以后的报告强制（旧报告不追溯）
    m = DATE_PAT.search(os.path.basename(path))
    if m and m.group(1) >= TAG_CUTOFF and not TAG_PAT.search(txt):
        missing.append("样本分布变更点提醒（change tag，见 change_tags.py）")
    return (not missing), missing


def main():
    ap = argparse.ArgumentParser(description="复盘报告「金额口径四情景」校验")
    ap.add_argument("report", nargs="?", help="复盘报告路径（reviews/YYYY-MM-DD-review*.md）")
    ap.add_argument("--all", metavar="DIR", help="校验目录下全部 *review*.md")
    ap.add_argument("--no-diag", action="store_true", help="只校验四情景、不校验三项诊断")
    args = ap.parse_args()

    targets = []
    if args.all:
        targets = sorted(glob.glob(os.path.join(args.all, "*review*.md")))
    elif args.report:
        targets = [args.report]
    else:
        ap.error("需要 <报告路径> 或 --all <目录>")

    bad = 0
    for p in targets:
        ok, missing = check(p, require_diag=not args.no_diag)
        name = os.path.basename(p)
        if ok:
            print(f"✅ {name}：四情景 + 诊断齐全")
        else:
            bad += 1
            print(f"❌ {name}：缺 {len(missing)} 项 —— " + "、".join(missing))
    if bad:
        print(f"\n提示：先跑 `python3 .pi/skills/trade/scripts/review.py <trades.csv> --md-block`，"
              f"把输出块整段粘进复盘报告，再补齐四情景的逐条解释。")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
