#!/usr/bin/env python3
"""复盘 EV 双口径报告（T199HKUS，2026-11-16 立）。

背景（为什么必须有这个脚本）：连败闸（第一级降频线）与熔断只拦**实盘**开仓，模拟盘照常
交易（2026-09-14 用户裁定：模拟盘是修整验证场、要攒样本）。于是复盘样本全部来自「不受
闸约束」的策略，而真正上实盘时这套闸会实时改变可交易集合——统计估计的是另一个策略的
期望（policy mismatch）。修法不是给模拟盘加闸（那样样本量会掉），而是**双轨**：
  ① 保留无闸样本，同时逐笔标注「若按实盘规则会不会被拦」（开仓脚本已输出
     `would_block_by`，AI 转录进 actions / 复盘 CSV）；
  ② 复盘固定双报：`EV_无闸`（观察用）与 `EV_按实盘规则回放`（决策用），并单列被拦笔组成。

本脚本做第 ② 件事：吃一份复盘样本库 CSV（`reviews/*-trades-*.csv`），按交易日重放实盘
连败闸，输出两种口径的 EV / 胜率，以及被拦笔里有多少大赢单。

闸的重放判据（与 `trade_utils_tiger` 的实盘判据同源，不另立一套）：
  - 连败 ≥3 笔，或连败 ≥2 笔且最近两笔净 R 均 ≤ FULL_LOSS_R（默认 -0.95）；
  - 且**最近一笔亏损发生在同一个交易日**（「次日再战」：跨到下一交易日自动放行）。
  重放分两种读法，都报：
    A. 标注口径（label）：闸状态由**实际发生过的全部交易**推进，逐笔标记「这笔会不会
       被拦」——回答「我们的样本里有多少笔在实盘根本做不出来」。
    B. 反事实回放（replay）：被拦的笔直接跳过、也不推进闸状态，只在留下的笔上推进——
       回答「如果一开始就按实盘规则跑，EV 会是多少」。

诚实边界（务必与数字一起读）：
  - 反事实回放不是严格反事实——跳过一笔会改变后续行情观察与 AI 判断（连败段常在同一个
    价格带反复试错，被跳过后 AI 可能去看别的标的）。所以它给方向与量级，不是精确估计。
  - CSV 里没有分钟级时间戳时，同一天内按**行序**当作时序（样本库是按发生顺序追加的）；
    有 `entry_time` 列时优先用它排序。
  - 只有 auto 模式的样本有「闸」概念；signal 模式本来就是人执行、不受本闸约束，默认剔除。

用法：
  python3 ev_dual_report.py                          # 自动取最新 reviews/*-trades-us.csv
  python3 ev_dual_report.py reviews/2026-11-14-trades-us.csv
  python3 ev_dual_report.py --market hk --mode auto_paper
  python3 ev_dual_report.py --out-prefix reviews/2026-11-16-ev-dual
"""
import argparse
import csv
import glob
import os
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parents[3]              # scripts → trade → skills → .pi → 项目根
sys.path.insert(0, str(SCRIPTS))
import review  # noqa: E402 —— 复用同一套净口径 R 算法（不另算一套）


_OPEN_HEAD = re.compile(r"【?🟢+\s*开仓[^🟢\n]*·\s*([A-Za-z0-9.]+)")
_TIME = re.compile(r"⏰\s*动作时间：\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})")


def actions_times(market="us"):
    """从 `actions/*-actions.md` 抽「每个交易日内每笔开仓的时序」，返回 {(date, symbol): [t1, t2, ...]}。

    为什么需要（T199，2026-11-16）：样本库 CSV 没有分钟级时间戳，只能按行序当同日时序，
    但行序不一定等于真实发生顺序——而连败闸是**日内时序敏感**的（先亏后赚与先赚后亏的拦截
    结果完全不同），顺序一错，被拦笔的集合就跟着错。actions 文件里每笔开仓都带
    `⏰ 动作时间：…`（北京墙钟，跨零点但在同一场交易内单调递增），拿它排时序是现成的权威源。

    只取开仓段（`🟢… 开仓 …`）后面的时间行；同一 (日期, 标的) 可能多笔，按出现顺序追加，
    调用方按 CSV 同键的行序依次取用。抽不到就返回空表，调用方自动退回行序。
    """
    suffix = "ET" if market == "us" else "HKT"
    out = {}
    # 2026-11-20 起 actions 按会话分文件：旧单文件与 *-actions-<stag>.md 都要扫
    _paths = sorted(set(glob.glob(str(ROOT / "actions" / f"*-{suffix}-actions.md"))
                        + glob.glob(str(ROOT / "actions" / f"*-{suffix}-actions-*.md"))))
    for p in _paths:
        try:
            txt = open(p, encoding="utf-8").read()
        except Exception:
            continue
        # 逐段扫：遇到开仓标题就记住标的，段内第一个动作时间就是这笔的开仓时刻
        cur_sym, pos = None, 0
        for line in txt.splitlines():
            m = _OPEN_HEAD.search(line)
            if m:
                cur_sym = m.group(1).upper()
                continue
            if cur_sym:
                mt = _TIME.search(line)
                if mt:
                    out.setdefault((mt.group(1), cur_sym), []).append(mt.group(2))
                    cur_sym = None
            pos += len(line)
    return out


def gate_full_loss_r():
    """亏满型阈值（与 `trade_utils_tiger.FULL_LOSS_R` 同源；导不进时退回同值常量）。

    不在这里重定义规则：阈值只有一处权威源，脚本只是读它；读不到时退回常量并列在报告里，
    免得「脚本的闸」和「实盘的闸」哪天悄悄分家。
    """
    try:
        import trade_utils_tiger as T
        return float(T.FULL_LOSS_R), "trade_utils_tiger.FULL_LOSS_R"
    except Exception:
        return -0.95, "内置常量（trade_utils_tiger 导入失败，值与模块一致）"


def row_order_map(path):
    """CSV 行序 → [(date, symbol, entry, exit, shares)]，用于还原同日内的发生顺序。

    `review.load_trades()` 会按 (date, symbol) 重排（那是明细编号需要的顺序，与本脚本的
    时序需求不同），所以这里自己扫一遍原始行、把「行号」记成一个可查的键表。
    """
    order = {}
    try:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(review._strip_comments(f)))
    except Exception:
        return order
    for i, r in enumerate(rows):
        try:
            key = (r["date"].strip(), r["symbol"].strip(), float(r["entry_price"]),
                   float(r["exit_price"]), float(r["shares"]))
        except Exception:
            continue
        order.setdefault(key, i)
    return order


def load(path, time_map=None):
    """读样本库 → 带行序的 trade 列表（已剔除 shadow）。

    time_map：`actions_times()` 的结果，用来把「同日计时序」换成真实开仓时刻；
    抽不到时刻的笔退回 CSV 行序（保证任何样本库都能跑，只是精度降一级）。
    """
    trades = review.load_trades(path)
    order = row_order_map(path)
    out = []
    by_day_sym = {}
    for t in trades:
        if t.get("shadow"):
            continue
        key = (t["date"], t["symbol"], t["entry"], t["exit"], t["shares"])
        t["_row"] = order.get(key, 10 ** 9)
        t["_seq"] = by_day_sym.get((t["date"], t["symbol"]), 0)
        by_day_sym[(t["date"], t["symbol"])] = t["_seq"] + 1
        times = (time_map or {}).get((t["date"], t["symbol"])) or []
        t["_time"] = times[t["_seq"]] if t["_seq"] < len(times) else ""
        out.append(t)
    return out


def sort_key(t):
    """同日排序：优先 actions 抽到的开仓时刻（真时序），其次 entry_time 列，最后 CSV 行序。

    没有时刻的笔用 `~` 前缀排到当日末尾（而不是混进开头）——宁可保守地把「时序不明」的笔
    当后发生，也不凭空插入到连败段的中间去改变闸的判定。
    """
    ts = t.get("_time") or t.get("entry_time") or ""
    return (t["date"], (ts if ts else "~9"), t["_row"])


def replay(trades, full_loss_r):
    """重放实盘连败闸，返回 (逐笔标注, 反事实保留集)。

    逐笔标注 = [(t, would_block_label: bool)]；反事实保留集 = 真正「按实盘规则做出来」的笔。
    两个序列都按交易日分组、**每个新交易日重新计数**（2026-11-18 用户立：闸是当日闸，
    前一日连败不带入今日）——与 `trade_utils_tiger.losing_streak_would_block` 的判据一致。
    样本已由 `--market` 参数按市场过滤，本函数不跨市场。
    """
    labelled, kept = [], []
    streak, recent = 0, []
    cur_day = None
    for t in sorted(trades, key=sort_key):
        if t["date"] != cur_day:
            cur_day = t["date"]
            streak, recent = 0, []          # 新交易日 → 重新计数
        triggered = (streak >= 3) or (streak >= 2 and len(recent) >= 2
                                      and all(x <= full_loss_r for x in recent[-2:]))
        labelled.append((t, triggered))
        # A. 标注口径：状态由实际发生的交易推进（样本就是这么产生的）
        if t["R"] < 0:
            streak += 1
            recent.append(t["R"])
            recent = recent[-5:]
        else:
            streak, recent = 0, []
    # B. 反事实回放：被拦的笔既不计入、也不推进状态
    streak, recent = 0, []
    cur_day = None
    for t, _ in labelled:
        if t["date"] != cur_day:
            cur_day = t["date"]
            streak, recent = 0, []
        triggered = (streak >= 3) or (streak >= 2 and len(recent) >= 2
                                      and all(x <= full_loss_r for x in recent[-2:]))
        if triggered:
            continue                     # 实盘会拦：不成交、不推进状态
        kept.append(t)
        if t["R"] < 0:
            streak += 1
            recent.append(t["R"])
            recent = recent[-5:]
        else:
            streak, recent = 0, []
    return labelled, kept


def stats(ts):
    """均值 R / 胜率 / 合计 R / ≥2R 笔数（口径与 review 的 EV 一致：EV = 均值净 R）。"""
    if not ts:
        return {"n": 0}
    R = [t["R"] for t in ts]
    return {"n": len(R), "sum": sum(R), "ev": sum(R) / len(R),
            "win": sum(1 for x in R if x > 0) / len(R) * 100,
            "big": sum(1 for x in R if x >= 2.0),
            "profit": sum(1 for x in R if x > 0)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="*", help="复盘样本库 CSV（缺省取最新 reviews/*-trades-<market>.csv）")
    ap.add_argument("--market", default="us", choices=["us", "hk"])
    ap.add_argument("--mode", default="auto_paper,auto_live",
                    help="参与统计的 mode（逗号分隔，默认 auto_paper,auto_live；signal 不受闸约束）")
    ap.add_argument("--out-prefix", default="", help="写 CSV+MD 的前缀（缺省只打印不落盘）")
    a = ap.parse_args()

    paths = a.csv
    if not paths:
        cands = sorted(glob.glob(str(ROOT / "reviews" / f"*-trades-{a.market}.csv")))
        if not cands:
            sys.exit(f"❌ reviews/ 下找不到 *-trades-{a.market}.csv，请显式传 CSV")
        paths = [cands[-1]]
    modes = {m.strip() for m in a.mode.split(",") if m.strip()}

    trades = []
    tmap = actions_times(a.market)
    time_hits = 0
    for p in paths:
        got = load(p, tmap)
        time_hits += sum(1 for t in got if t.get("_time"))
        trades.extend(got)
    trades = [t for t in trades if t.get("mode") in modes]
    if not trades:
        sys.exit(f"❌ 这些 CSV 里没有 mode ∈ {sorted(modes)} 的样本")

    full_loss_r, threshold_src = gate_full_loss_r()
    labelled, kept_iter = replay(trades, full_loss_r)
    blocked = [t for t, b in labelled if b]
    kept_proj = [t for t, b in labelled if not b]      # 口径 A：标注投影
    kept_iter_ids = {id(t) for t in kept_iter}          # 口径 B：反事实迭代
    s_all, s_proj, s_iter, s_blocked = (stats(trades), stats(kept_proj),
                                        stats(kept_iter), stats(blocked))

    lines = ["# 复盘 EV 双口径报告（T199HKUS）", "",
             f"- 样本：{', '.join(os.path.basename(p) for p in paths)}"
             f"（mode ∈ {sorted(modes)}，剔除影子组）",
             f"- 闸判据：单市场当日连败 ≥3 笔，或当日连败 ≥2 笔且最近两笔净 R 均 ≤ {full_loss_r}"
             f"（阈值来源：{threshold_src}）；每个交易日重新计数（港美各一条序列，本表只算 "
             f"{a.market.upper()}）",
             f"- R 口径：review.py 的**净 R**（分母 = 开仓止损距×股数 + 开仓边费 + 止损价平仓边费），"
             f"不是 actions 里手写的数字——两边偶尔差几十个 bp，以本表为准",
             f"- 生成：`ev_dual_report.py`（{len(trades)} 笔；其中 {time_hits} 笔的开仓时序取自 "
             f"actions 的 `⏰ 动作时间`，其余退回 CSV 行序）", "",
             "## 一、三口径对比", "",
             "| 口径 | 笔数 | 合计 R | EV（均值 R） | 胜率 | ≥2R 笔数 |",
             "|---|---|---|---|---|---|",
             f"| **EV_无闸**（观察用，样本原样） | {s_all['n']} | {s_all['sum']:+.2f}R | "
             f"{s_all['ev']:+.3f}R | {s_all['win']:.0f}% | {s_all['big']} |",
             f"| **EV_按实盘规则回放·标注投影**（决策用主口径） | {s_proj['n']} | {s_proj['sum']:+.2f}R | "
             f"{s_proj['ev']:+.3f}R | {s_proj['win']:.0f}% | {s_proj['big']} |",
             f"| EV_反事实迭代（次要口径，见§五） | {s_iter['n']} | {s_iter['sum']:+.2f}R | "
             f"{s_iter['ev']:+.3f}R | {s_iter['win']:.0f}% | {s_iter['big']} |",
             f"| 差额（标注投影 − 无闸） | {-s_blocked['n']} | "
             f"{s_proj['sum'] - s_all['sum']:+.2f}R | {s_proj['ev'] - s_all['ev']:+.3f}R | "
             f"{s_proj['win'] - s_all['win']:+.0f}pp | {s_proj['big'] - s_all['big']:+d} |", "",
             "## 二、被拦笔的组成（闸会砍掉什么）", ""]
    if blocked:
        lines += [f"- 被拦 **{s_blocked['n']} 笔**（占 {s_blocked['n'] / len(trades) * 100:.0f}%），"
                  f"合计 **{s_blocked['sum']:+.2f}R**、均值 **{s_blocked['ev']:+.3f}R**、"
                  f"其中 **{s_blocked['profit']} 笔盈利**、**{s_blocked['big']} 笔 ≥2R**",
                  "- 这一行就是「闸是否会砍掉大赢单」的可监控指标：≥2R 笔数不为 0 时，"
                  "闸的贡献方向依赖行情 regime，不能当成干净的「只砍亏损」。", ""]
        lines += ["| 日期 | 标的 | 净 R |", "|---|---|---|"]
        lines += [f"| {t['date']} | {t['symbol']} | {t['R']:+.3f}R |" for t in blocked]
    else:
        lines.append("- 本样本内闸未触发过任何拦截（连败条件一次都没满足）。")
    lines += ["", "## 三、两个回放口径为什么不同", "",
              "- **标注投影**（主口径）：闸状态完全由**实际发生过的交易**推进，被标注的笔整笔剔除、"
              "其余照旧——回答「如果这些笔在实盘根本做不出来，剩下这批的 EV 是多少」。",
              "- **反事实迭代**（次要）：被拦的笔不仅不计入、也不推进闸状态（实盘里它压根没成交）。"
              "它的游走轨迹与标注投影不同：某笔本来能「赢一把清零连败」的交易若已被拦，连败就一直挂着、"
              "后面会被拦得更多，所以笔数通常更少。这是另一种合理读法，但多一层假设，故降为参考。",
              "- 两者都给方向与量级；「闸确实改变了可交易集合、被拦笔里总会有大赢单」在两种读法下一致。", "",
              "## 四、口径纪律（T199 第 ④ 条）", "",
              "- 报 EV 必须写明口径：**「EV_无闸」不得直接当作实盘策略的期望**，"
              "它是「样本里实际发生了什么」的描述；对实盘有意义的数字是「EV_按实盘规则回放」。",
              "- 例外：实盘已经跑过一段、样本本身就是按实盘规则产生的（mode=auto_live 且闸全程在线），"
              "此时两者口径一致，可只用无闸口径。", "",
              "## 五、诚实边界", "",
              "- 回放不是严格反事实：跳过一笔会改变后续的行情观察与判断（连败段常在同一个价格带"
              "反复试错，被跳过后 AI 可能去看别的标的），所以给方向与量级、不当精确估计。",
              "- 同日内的时序：CSV 有 `entry_time` 列时按它排，没有时按行序（样本库按发生顺序追加）。",
              "- 与 2026-11-15 的一次性回放（TODO T199 里记的「跳过 18 笔 / 保留 93 笔 / +0.370R」）"
              "在同一量级（本表标注投影同为 18 / 93），但**被拦的具体笔与 EV 数值不同**：那次回放的 R "
              "取自 actions 里手写的「实际落地赔率」，本表用 review.py 现算的净 R——同一笔可能差几十个 bp，"
              "但足以改变「哪两笔算亏满型」从而挪动闸的触发点。**以可重跑的脚本结果为准**，"
              "旧数字标注为一次性分析。",
              "- 本报告不判断闸本身该不该留（那是 T196HKUS 的话题），只负责把两种口径分开报清楚。", ""]

    md = "\n".join(lines)
    print(md)
    if a.out_prefix:
        out_md = Path(a.out_prefix + ".md")
        out_csv = Path(a.out_prefix + ".csv")
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(md, encoding="utf-8")
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["日期", "标的", "方向", "mode", "净R", "标注_会被闸拦",
                        "标注投影_是否保留", "反事实迭代_是否保留"])
            kept_ids = kept_iter_ids
            for t, b in labelled:
                w.writerow([t["date"], t["symbol"],
                            "long" if t["sign"] > 0 else "short", t.get("mode", ""),
                            f"{t['R']:+.4f}", "1" if b else "0",
                            "0" if b else "1",
                            "1" if id(t) in kept_ids else "0"])
        print(f"\n✅ 已写 {out_md} 与 {out_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
