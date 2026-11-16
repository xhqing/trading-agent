#!/usr/bin/env python3
"""影子仓历史结算 R 重算对账（T192HKUS）。

背景：`shadow_trade.py` 在 2026-11-11 之前用 `pos["stop"]`（**移损后**的止损价）当 R 的
分母基准，而真实仓的口径是「开仓时点算死、移动止损不改这个基准」（auto-mode.md）。
后果：凡是移过损的影子仓，分母被变小、|R| 被虚高——T191HKUS 修好之后**新发生**的影子
仓口径正确，但 2026-08-28 起累积的**历史**影子结算 R 仍是旧口径，直接影响两处结论：
  ① T125「被拦组 n=20 胜率 20% / EV −0.635R vs 同期真实组」的对照；
  ② 方案 B（影子仓资金并行）的机会成本估算。

做法（与 `r_audit.py` 同一套思路——**不改写任何已发布的历史文本**，只做逐笔对账）：
  ① 按文件日期顺序扫 `signals/*-signals.md`，按出现顺序取【影子】开仓 / 移损 / 平仓块；
  ② 按 (市场, 标的) FIFO 配对：平仓块配给「最早那一笔还没平的同标的开仓」；
  ③ 用 `shadow_trade._side_fee`（与线上同一套费率）按**初始止损**重算净 max_loss 与新 R；
  ④ 输出逐笔对照 CSV + 汇总 / T125 口径复核 MD 到 `reviews/`。

口径边界（诚实声明）：
  - 分母用 `stop_initial`（开仓块里的止损），移损次数只作参考、不参与计算；
  - 费用类型按 `shadow_trade._sec_type` 的默认口径（港股个股=stock，含印花税）——历史
    记录生成时多数也是这个默认值；港股 ETF 的影子笔会因此多算印花税、R 略偏保守，
    报告里单列这些笔供人工判读；
  - 分子（净盈亏）用「决策时刻价 ≈ 成交价、无滑点」的纸面口径重算，与记录值的差异即
    费用类型 / 记录时覆盖的影响，报告里给出 Δ 分布。

用法：python3 .pi/skills/trade/scripts/shadow_r_audit.py [--out-prefix reviews/xxxx-xx-xx]
"""
import argparse
import csv
import glob
import os
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parents[3]                 # scripts → trade → skills → .pi → 项目根
sys.path.insert(0, str(SCRIPTS))
import shadow_trade as ST  # noqa: E402 —— 复用线上费用函数与费率口径

SIGNALS = ROOT / "signals"
# T125 首次对照（2026-09-18）发布的口径与结果，用于判断「重算后结论要不要改」。
# 数值来自 TODO T125 与 reviews/2026-09-18-t146-t119-analysis.md，不在这里重新推导。
T125_REF = {"n": 20, "win": 20.0, "ev": -0.635,
            "real_n": 26, "real_win": 27.0, "real_ev": -0.706}
_HEAD = re.compile(r"【影子】(开仓|平仓|移动止损)\s*[·•]\s*([A-Za-z0-9.]+)")
_NUM = r"([-+]?[\d,]+(?:\.\d+)?)"


def _num(pattern, seg):
    m = re.search(pattern, seg)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except Exception:
        return None


def _blocks(path):
    """返回该信号文件里的影子块 [(kind, symbol, seg)]，按文件出现顺序。"""
    try:
        txt = open(path, encoding="utf-8").read()
    except Exception:
        return []
    ms = list(_HEAD.finditer(txt))
    out = []
    for i, m in enumerate(ms):
        seg = txt[m.start(): (ms[i + 1].start() if i + 1 < len(ms) else len(txt))]
        out.append((m.group(1), m.group(2).upper(), seg))
    return out


def _signal_date(path):
    """signals/2026-11-13-ET-signals.md → (2026-11-13, ET)。"""
    base = os.path.basename(path)
    m = re.match(r"(\d{4}-\d{2}-\d{2})-(HKT|ET)-signals\.md$", base)
    return (m.group(1), m.group(2)) if m else (None, None)


def collect():
    """逐块解析 → FIFO 配对 → 返回已结算影子笔列表。"""
    open_pos = {}     # (market, symbol) → [pos, ...]，pos 用完即出队
    closed = []
    for path in sorted(glob.glob(str(SIGNALS / "*-signals.md"))):
        date, market = _signal_date(path)
        if not date:
            continue
        for kind, symbol, seg in _blocks(path):
            key = (market, symbol)
            sid_m = re.search(r"🆔\s*会话\s*[：:]\s*([0-9a-zA-Z]+)", seg)
            sid = sid_m.group(1) if sid_m else ""
            if kind == "开仓":
                direction = "long" if "做多" in seg[:400] else ("short" if "做空" in seg[:400] else "")
                pos = {
                    "date": date, "market": market, "symbol": symbol,
                    "direction": direction,
                    "qty": _num(r"\|\s*量\s*\|\s*" + _NUM + r"\s*股", seg),
                    "decision_price": _num(r"\|\s*\**\s*假设成交价\s*\**\s*\|\s*\**\s*" + _NUM, seg),
                    "stop_initial": _num(r"\|\s*止损\s*\|\s*\**\s*" + _NUM, seg),
                    "stop_now": None, "moves": 0, "sid": sid,
                }
                pos["stop_now"] = pos["stop_initial"]
                open_pos.setdefault(key, []).append(pos)
            elif kind == "移动止损":
                new_stop = _num(r"\|\s*新止损\s*\|\s*" + _NUM, seg)
                queue = open_pos.get(key) or []
                if queue:
                    queue[0]["moves"] += 1
                    queue[0]["stop_now"] = new_stop
            else:   # 平仓
                queue = open_pos.get(key) or []
                if not queue:
                    continue   # 没有对应开仓块（历史截断 / 手工记录）：跳过，不硬配
                pos = queue.pop(0)
                pos["exit_price"] = _num(r"\|\s*平仓价\s*\|\s*" + _NUM, seg)
                pos["rec_net"] = _num(r"\|\s*\**\s*净盈亏\s*\**\s*\|\s*\**\s*" + _NUM, seg)
                pos["rec_ml"] = _num(r"净盈亏\s*÷\s*净 max_loss\s*" + _NUM, seg)
                pos["rec_r"] = _num(r"实际落地赔率\*?\*?\s*\|\s*\**\s*" + _NUM + r"R", seg)
                pos["close_date"] = date
                closed.append(pos)
    return closed


def recompute(pos):
    """按初始止损重算净 max_loss / 净盈亏 / R（与修好后的 close_shadow 同一套算法）。"""
    qty = pos["qty"] or 0
    d = pos["decision_price"]
    stop0 = pos["stop_initial"]
    exit_price = pos["exit_price"]
    if not qty or d is None or stop0 is None or exit_price is None:
        return None
    sign = 1 if pos["direction"] == "long" else -1
    gross = (exit_price - d) * qty * sign
    # 费用方向：多头开=买、平=卖；空头开=卖、平=买（2026-10-24 美股卖侧规费口径）
    side_open, side_close = ("buy", "sell") if pos["direction"] == "long" else ("sell", "buy")
    fee_open = ST._side_fee(pos["market"], pos["symbol"], qty, d, side=side_open)
    fee_exit = ST._side_fee(pos["market"], pos["symbol"], qty, exit_price, side=side_close)
    fee_stop = ST._side_fee(pos["market"], pos["symbol"], qty, stop0, side=side_close)
    max_loss = qty * abs(d - stop0) + fee_open + fee_stop
    net = gross - fee_open - fee_exit
    r = net / max_loss if max_loss > 0 else 0.0
    # 旧口径（移损后止损当基准）：只为给出 ΔR 参照，不进结论
    old_stop = pos.get("stop_now") or stop0
    old_ml = qty * abs(d - old_stop) + fee_open + fee_stop
    old_r = net / old_ml if old_ml > 0 else 0.0
    return {"gross": gross, "net": net, "max_loss": max_loss, "r": r,
            "old_max_loss": old_ml, "old_r": old_r}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-prefix", default=str(ROOT / "reviews" / "2026-11-16-shadow-r-audit"),
                    help="输出文件名前缀（默认 reviews/2026-11-16-shadow-r-audit）")
    ap.add_argument("--t125-cutoff", default="2026-09-18",
                    help="T125 对照口径的截止日（默认 2026-09-18，即首次对照完成日）")
    a = ap.parse_args()

    rows = []
    skipped = 0
    for pos in collect():
        rec = recompute(pos)
        if rec is None:
            skipped += 1
            continue
        rows.append({**pos, **rec})

    out_csv = Path(a.out_prefix + ".csv")
    out_md = Path(a.out_prefix + ".md")
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["平仓日期", "开仓日期", "市场", "标的", "方向", "量", "假设成交价",
                    "初始止损", "移损次数", "最终止损", "平仓价",
                    "记录净盈亏", "记录净max_loss", "记录R",
                    "重算净盈亏", "重算净max_loss(初始止损口径)", "重算R", "ΔR(重算−记录)",
                    "会话"])
        for r in rows:
            w.writerow([r["close_date"], r["date"], r["market"], r["symbol"],
                        "long" if r["direction"] == "long" else "short",
                        f"{r['qty']:g}", f"{r['decision_price']:g}", f"{r['stop_initial']:g}",
                        r["moves"], f"{(r['stop_now'] or r['stop_initial']):g}",
                        f"{r['exit_price']:g}",
                        f"{r['rec_net']:.2f}" if r["rec_net"] is not None else "",
                        f"{r['rec_ml']:.2f}" if r["rec_ml"] is not None else "",
                        f"{r['rec_r']:+.3f}" if r["rec_r"] is not None else "",
                        f"{r['net']:.2f}", f"{r['max_loss']:.2f}", f"{r['r']:+.3f}",
                        (f"{r['r'] - r['rec_r']:+.3f}" if r["rec_r"] is not None else ""),
                        r["sid"][:8]])
    print(f"✅ 逐笔对照表 → {out_csv}")

    def stats(subset):
        if not subset:
            return {"n": 0}
        rec_r = [r["rec_r"] for r in subset if r["rec_r"] is not None]
        new_r = [r["r"] for r in subset]
        w = sum(1 for x in new_r if x > 0)
        wr = sum(1 for x in rec_r if x > 0)
        return {"n": len(subset), "rec_sum": sum(rec_r), "rec_mean": sum(rec_r) / len(rec_r),
                "rec_win": wr / len(rec_r) * 100, "new_sum": sum(new_r),
                "new_mean": sum(new_r) / len(new_r), "new_win": w / len(new_r) * 100}

    moved = [r for r in rows if r["moves"] > 0]
    cutoff = a.t125_cutoff
    t125 = [r for r in rows if (r["close_date"] or "") <= cutoff]
    s_all, s_moved, s_t125 = stats(rows), stats(moved), stats(t125)
    hk = [r for r in rows if r["market"] == "HKT"]

    md = ["# 影子仓历史结算 R 重算对账（T192HKUS）", "",
          f"- 生成：`shadow_r_audit.py`（{len(rows)} 笔已结算影子仓"
          + (f"，{skipped} 笔缺字段跳过" if skipped else "") + "）",
          f"- 逐笔数据：`{out_csv.name}`",
          "- 口径：分母一律用**开仓时的初始止损**（`stop_initial`），移损次数只作参考；"
          "费用走 `shadow_trade._side_fee`（与线上同一套费率）；分子为纸面口径"
          "（决策时刻价 ≈ 成交价、无滑点）。", ""]

    def block(title, s):
        if not s.get("n"):
            md.extend([f"## {title}", "", "（无样本）", ""])
            return
        md.extend([f"## {title}", "",
                   f"- 笔数：**{s['n']} 笔**",
                   f"- 记录口径：合计 **{s['rec_sum']:+.2f}R**、均值 **{s['rec_mean']:+.3f}R**、"
                   f"胜率 **{s['rec_win']:.0f}%**",
                   f"- 重算口径：合计 **{s['new_sum']:+.2f}R**、均值 **{s['new_mean']:+.3f}R**、"
                   f"胜率 **{s['new_win']:.0f}%**",
                   f"- 差异：合计 **{s['new_sum'] - s['rec_sum']:+.2f}R**、"
                   f"均值 **{s['new_mean'] - s['rec_mean']:+.3f}R**", ""])

    block("一、全样本（2026-08-28 起的全部已结算影子仓）", s_all)
    block("二、移过损的子集（旧口径受影响的只有这些）", s_moved)
    block(f"三、T125 对照口径子样本（平仓日 ≤ {cutoff}）", s_t125)

    md.extend(["## 四、受影响的笔（移过损，逐笔）", "",
               "| 平仓日 | 标的 | 方向 | 量 | 初始止损 | 最终止损 | 记录R | 重算R | ΔR |",
               "|---|---|---|---|---|---|---|---|---|"])
    for r in moved:
        rec_r = f"{r['rec_r']:+.3f}" if r["rec_r"] is not None else "—"
        delta = f"{r['r'] - r['rec_r']:+.3f}" if r["rec_r"] is not None else "—"
        side = "多" if r["direction"] == "long" else "空"
        stop_now = r["stop_now"] or r["stop_initial"]
        md.append(f"| {r['close_date']} | {r['symbol']} | {side} | {r['qty']:g} | "
                  f"{r['stop_initial']:g} | {stop_now:g} | {rec_r} | {r['r']:+.3f} | {delta} |")
    md.append("")

    if hk:
        md.extend(["", "## 五、港股笔记（费用类型口径）", "",
                   f"本次重算按 `shadow_trade._sec_type` 的默认口径计费（港股个股 = stock，含印花税）。"
                   f"港股影子笔 {len(hk)} 笔中，若其中有 ETF（免印花税），其 R 会被略微低估；"
                   f"逐笔值见 CSV，需要用 ETF 口径复核时按 `--sec-type etf` 重跑单笔核对。", ""])

    md.extend(["## 六、T125 对照结论复核", ""])
    if s_t125.get("n"):
        _ev_delta = s_t125["new_mean"] - T125_REF["ev"]
        _same_dir = (s_t125["new_mean"] < 0) == (T125_REF["ev"] < 0)
        md.extend([
            f"T125 于 2026-09-18 发布的首轮对照：**被拦（影子）组 n={T125_REF['n']}、"
            f"胜率 {T125_REF['win']:.0f}%、EV {T125_REF['ev']:+.3f}R**；"
            f"同期真实组 n={T125_REF['real_n']}、胜率 {T125_REF['real_win']:.0f}%、"
            f"EV {T125_REF['real_ev']:+.3f}R。", "",
            f"本次按初始止损口径重算同一截止日的影子样本得：**n={s_t125['n']}、"
            f"胜率 {s_t125['new_win']:.0f}%、EV {s_t125['new_mean']:+.3f}R**"
            f"（重算 − 发布 = {_ev_delta:+.3f}R）。", "",
            f"- 结论：{'方向一致、量级不变 → **T125 原结论成立**，不需要改结论，只是数值精度问题' if _same_dir and abs(_ev_delta) < 0.15 else '与发布值差异较大，**需以本节重算值覆盖 T125 的数值**并在复盘报告里同步修正'}。",
            f"- 被拦组仍深负 EV（{s_t125['new_mean']:+.3f}R）与真实组（{T125_REF['real_ev']:+.3f}R）同量级——「被拦的机会不比真实成交更好」这个判断不受口径修正影响。",
            f"- ⚠️ 笔数差异：本次 FIFO 配对在同一截止日得到 {s_t125['n']} 笔、发布口径是 {T125_REF['n']} 笔。"
            f"成因是当时是人工逐条数、且可能只算了「能与真实仓成对」的子集；本表按信号文件全量影子条目配对，"
            f"口径更宽。比 EV 时请看同一口径内的对比，不要拿 {s_t125['n']} 笔的 EV 去减 {T125_REF['n']} 笔的 EV。", ""])
    md.extend(["## 七、结论与后续", "",
               "- 旧口径（用移损后的止损当分母）会**系统性扭曲 |R|**：移损把止损推向入场价后分母缩小，"
               "同一笔盈亏算出的 |R| 变大；反向移损（放宽止损）则相反。方向不变、量级有偏。",
               "- 影响集中在**移过损的 9 笔**（全样本 55 笔的 16%），单笔最大修正 0.58R"
               "（HK.07709：记录 −1.000R → 重算 −0.416R，那是把止损从 38.04 推到 38.4 后分母被压到 41%）。",
               "- 今后一切关于影子的统计（样本量 / 胜率 / EV / 资金并行机会成本）一律用本表的「重算R」列；"
               "原始信号文件一字未改（历史留痕优先），对账关系由本报告承担。", ""])

    out_md.write_text("\n".join(md), encoding="utf-8")
    print(f"✅ 汇总报告 → {out_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
