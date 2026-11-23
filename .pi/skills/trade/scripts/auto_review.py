#!/usr/bin/env python3
"""停盯后自动复盘（T202HKUS，2026-11-18 用户立）。

## 为什么需要

复盘原本只能由用户手动触发，而用户并不按固定频率触发 → 两道实盘闸（一级连败闸 / 二级
熔断闸）的状态长期没人定期核对：闸已经拦单了没人知道该修整，或者已经熔断没人提醒
「明天要么不交易、要么只能做模拟盘」。用户 2026-11-18 原话：「加一个功能：每天停止盯盘后
自动复盘，把复盘报告 MD 落盘，并汇报是否触发实盘的两种闸」。

## 做什么（每次停盯收尾跑一次，**实盘 / 模拟盘都跑**）

1. **补样本库**：把当日成交追加进 `reviews/<date>-trades-<market>.csv`（累积快照口径，
   与现有复盘 CSV 一致——每份都含截至当期全部真实样本，按 (date,symbol,entry,exit,shares)
   去重）。
2. **落复盘报告** `reviews/<date>-review-<market>.md`：当日战绩 + 两闸状态 + 处置建议。
3. **输出 JSON**：两闸状态与处置话术，供 `stop_summary.py` 写进飞书汇报。

## 两闸判据（与实现同源，不另立一套）

- **一级 · 连败闸**：`trade_utils_tiger._daily_streak_cell` + `_losing_streak_triggered`
  （单市场当日连败 ≥3，或 ≥2 且最近两笔都亏满型；港美各一条序列、每日重新计数）。
  作用域 = 实盘开仓。
- **二级 · 熔断闸**：`review.p_g_pos(R, f)` 与最近连续 N 笔有无盈利，阈值取
  `config.json risk.circuit_breaker`（默认 P(g>0) < 80% 且最近 5 笔无盈利）；当前熔断
  状态取 `state/circuit_breaker.json`。作用域同样只含实盘开仓。

## 用法

    python3 auto_review.py --market us                 # 当日（ET）
    python3 auto_review.py --market hk --date 2026-11-18
    python3 auto_review.py --market us --json-only     # 只要两闸状态（给 stop_summary 用）
    python3 auto_review.py --market us --no-csv        # 不补样本库

退出码：0 正常；1 数据不足（当日 0 笔且无历史样本）不算失败，仍会输出两闸状态。
"""

import argparse
import fcntl
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent.parent.parent.parent
sys.path.insert(0, str(SCRIPTS))

import review as RV                       # noqa: E402
import change_tags as CT                  # noqa: E402
import circuit_breaker as CB              # noqa: E402
import trade_utils_tiger as T             # noqa: E402

CSV_HEADER = "date,symbol,direction,entry_price,exit_price,shares,max_loss,type,mode,shadow"


def load_config():
    try:
        with open(SCRIPTS.parent / "config.json") as f:
            return json.load(f)
    except Exception:
        return {}


def futu_symbol(sym, market):
    """券商 symbol → 富途格式（US.AMD / HK.00700），review.py 需要带市场前缀。"""
    s = str(sym or "").strip()
    if s.startswith(("US.", "HK.")):
        return s
    return f"US.{s}" if market == "us" else f"HK.{s.zfill(5)}"


def latest_csv(market):
    cands = sorted((ROOT / "reviews").glob(f"*-trades-{market}.csv"))
    return cands[-1] if cands else None


def read_rows(path):
    """读累积样本 CSV → 行 dict 列表（跳过注释行、保留粗字段）。"""
    rows = []
    if not path or not Path(path).exists():
        return rows
    try:
        with open(path, encoding="utf-8") as f:
            head = None
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                cells = [c.strip() for c in line.rstrip("\n").split(",")]
                if head is None:
                    head = cells
                    continue
                rows.append(dict(zip(head, cells)))
    except Exception:
        pass
    return rows


def row_key(r):
    return (str(r.get("date")), str(r.get("symbol")), str(r.get("entry_price")),
            str(r.get("exit_price")), str(r.get("shares")))


EXCLUDE_FILE = "review_exclude.json"


def load_excludes():
    """异常样本排除清单（2026-11-22 立，T207US）。

    为什么需要：脚本 bug 造成的「凭空开仓」「下单即被止损平掉」这类成交不是策略决策的
    产物——进累积样本会污染 EV / 胜率 / R 分布，进而影响两闸判据（它们直接决定实盘开
    仓许可）。2026-11-20 实录两笔：① 滑点使成交价越过止损价、下单瞬间即被平掉（slip_R
    1.09）；② 止损腿补挂方向挂反、空仓下凭空开出 515 股裸空（T204US）。
    清单放 reviews/ 下（入库），异常样本可追溯、不随时间消失。
    """
    p = ROOT / "reviews" / EXCLUDE_FILE
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("entries") or []
    except Exception:
        return []


def is_excluded(row, excludes):
    """按 date + symbol + direction + shares + entry_price 匹配异常样本。

    用「开仓侧特征」匹配而非平仓价：未平仓的异常单在平仓后才会进样本，那时平仓价才存在，
    但开仓价与数量在事故当时就定了，用它能提前把风险锁死。
    """
    if not excludes:
        return False
    try:
        _d = str(row.get("date"))
        _s = str(row.get("symbol"))
        _dir = str(row.get("direction"))
        _q = int(float(row.get("shares") or 0))
        _px = float(row.get("entry_price") or 0)
    except Exception:
        return False
    for e in excludes:
        try:
            if (_d == str(e.get("date")) and _s == str(e.get("symbol"))
                    and _dir == str(e.get("direction"))
                    and _q == int(float(e.get("shares") or 0))
                    and abs(_px - float(e.get("entry_price") or 0)) < 0.02):
                return True
        except Exception:
            continue
    return False


def sec_type_map(rows):
    """symbol → 证券类型（stock / etf），从历史样本继承（review 用它定港股印花税）。"""
    m = {}
    for r in rows:
        s, t = r.get("symbol"), (r.get("type") or "").lower()
        if s and t in ("stock", "etf", "reit"):
            m[s] = t
    return m


def daily_rows(market, date, account="paper", known_types=None):
    """从券商订单生成当日成交行（review.py CSV 口径）。缺数据时返回空列表。

    止损价取 **actions 记录里的原始止损**（移损前的）——止损腿 aux_price 会被 move_stop
    改掉，用它算 max_loss 会把分母压小、R 虚高（stop_summary.original_stops 的同一理由）。
    """
    import stop_summary as SS
    tz_day = SS.market_day_tz(market)
    y, m, d = [int(x) for x in date.split("-")]
    start = datetime(y, m, d, 0, 0, tzinfo=tz_day)
    end = start + timedelta(days=1)
    orders, _masked = SS.fetch_orders(market, account, start, end)
    pairs = [t for t in SS.build_trades(orders) if t.get("entry_filled") and t.get("exit")]

    # 2026-11-20 起 actions 按会话分文件：原始止损要扫该日该市场的全部文件
    # （旧单文件 + 各会话分文件），再按「同标的 + 开仓价最接近」匹配。
    import actions_paths as _AP
    orig = []
    for _fp in _AP.day_files(market, date, str(ROOT)):
        orig.extend(SS.original_stops(_fp))

    out = []
    for t in pairs:
        sym = futu_symbol(t["symbol"], market)
        stop = None
        # 同标的可能一天多笔：取与开仓价最接近的那条原始止损记录
        cands = [(abs(e - t["entry"]), st) for s, e, st in orig if futu_symbol(s, market) == sym]
        if cands:
            stop = min(cands)[1]
        stop = stop or t.get("stop_leg")
        if not stop or not t.get("entry") or not t.get("qty"):
            continue
        ml = abs(float(t["entry"]) - float(stop)) * int(t["qty"])
        out.append({
            "date": date,
            "symbol": sym,
            "direction": t["direction"],
            "entry_price": f"{float(t['entry']):.4f}",
            "exit_price": f"{float(t['exit']):.4f}",
            "shares": str(int(t["qty"])),
            "max_loss": f"{ml:.2f}",
            "type": (known_types or {}).get(sym, "etf" if market == "us" else "stock"),
            "mode": "auto_paper" if account == "paper" else "auto_live",
            "shadow": "",
        })
    return out


def _atomic_write_text(path, text):
    """原子写：先写同目录 .tmp 再 os.replace（读者要么看到旧版、要么看到新版，不会读到半截）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class _ReviewLock:
    """跨进程互斥（flock）：同一市场同一时刻只让一个会话跑「读旧样本 → 合并 → 写样本 → 写报告」。

    为什么需要（2026-11-20 立）：停盯收尾由每个会话各自跑，两个会话几乎同时停时，
    A 正在 `open(...,"w")` 截断重写累积样本 CSV、B 恰好 `read_rows()` → 读到半截文件，
    合并后再写回就是**历史样本被截断**（累积库永久丢数据；且两闸判据吃错样本会误判）。
    锁文件放 state/（不入库），拿不到锁时等待（阻塞），退出时自动释放。
    """

    def __init__(self, name="auto_review.lock"):
        self.path = ROOT / "state" / name
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()
        return False


def write_csv(path, rows):
    # 原子写（2026-11-20）：累积样本库被多个会话并发读写时，绝不能让读者看到半截文件
    buf = [CSV_HEADER]
    for r in rows:
        buf.append(",".join(str(r.get(k, "")) for k in CSV_HEADER.split(",")))
    _atomic_write_text(Path(path), "\n".join(buf) + "\n")


def gates_status(market, date, csv_rows):
    """两闸状态。返回 dict（供报告与飞书共用）。"""
    cfg = load_config()
    risk = cfg.get("risk") or {}
    cbc = risk.get("circuit_breaker") or {}
    f = float(risk.get("risk_fraction", 0.02))
    floor = float(cbc.get("pg_pos_floor", 0.80))
    n_winless = int(cbc.get("recent_winless_required", 5))
    resume_min = int(cbc.get("resume_min_trades", 10))

    # ── 一级 · 连败闸（单市场当日）───────────────────────────────
    # 关键：要按**复盘的那个交易日**算（该市场时区的当日 23:59），不能用「现在」——
    # 否则跑历史日期复盘时会因已跨交易而被「每日重新计数」归零，看不出那天到底溦没溦。
    try:
        y, m, d = [int(x) for x in date.split("-")]
        as_of = datetime(y, m, d, 23, 59, tzinfo=T.market_tz(market))
    except Exception:
        as_of = None
    try:
        st = T._migrate_losing_streak(json.load(open(T._losing_streak_path())))
        cell = T._daily_streak_cell(st, market, as_of)
    except Exception:
        cell = {}
    l1 = {
        "streak": int(cell.get("streak", 0) or 0),
        "recent_r": list(cell.get("recent_r", []) or []),
        "date": cell.get("date"),
        "triggered": T._losing_streak_triggered(cell),
        "scope": "只拦实盘开仓；模拟盘照常",
    }

    # ── 二级 · 熔断闸（全样本该市场）────────────────────────────
    real = [r for r in csv_rows if str(r.get("shadow", "")).strip() not in ("1", "true", "True")]
    R_all = []
    for t in RV.load_trades(str(latest_csv(market))) if latest_csv(market) else []:
        if not t.get("shadow"):
            R_all.append(t["R"])
    pg = RV.p_g_pos(R_all, f) if R_all else {"P_pos": float("nan")}
    seq = [t for t in (RV.load_trades(str(latest_csv(market))) if latest_csv(market) else [])
           if not t.get("shadow")]
    seq = sorted(seq, key=lambda t: t.get("exit_time") or (t.get("date", "") + " " + t.get("symbol", "")))
    recent_r = [t["R"] for t in seq[-n_winless:]]
    cond_pg = bool(R_all) and bool(pg["P_pos"] < floor)
    cond_winless = len(recent_r) >= n_winless and all(x <= 0 for x in recent_r)
    cb_state = CB.read_state()
    l2 = {
        "pg_pos": None if not R_all else round(float(pg["P_pos"]), 4),
        "f": f,
        "n_trades": len(R_all),
        "floor": floor,
        "recent_winless_required": n_winless,
        "recent_r": [round(x, 3) for x in recent_r],
        "cond_pg": cond_pg,
        "cond_winless": cond_winless,
        "judged_triggered": cond_pg and cond_winless,
        "status": cb_state.get("status", "ok"),
        "tripped_at": cb_state.get("tripped_at"),
        "reason": cb_state.get("reason"),
        "resume_min_trades": resume_min,
        "scope": "只拦实盘开仓；模拟盘 / 信号照常（熔断期 = 修整验证场）",
    }
    return {"market": market, "date": date, "L1": l1, "L2": l2}


def advice(g):
    """处置话术（飞书 / 报告共用）。"""
    l1, l2 = g["L1"], g["L2"]
    out = []
    # 二级优先（熔断重于降频）
    if l2["status"] == "tripped":
        out.append(f"⛔ 实盘熔断中（{l2.get('tripped_at')} 触发）——明天起**实盘一律不开新仓**："
                   f"要么不交易，要么只做模拟盘交易；直到模拟盘新样本 ≥{l2['resume_min_trades']} 笔、"
                   f"段内 EV>0 且无 3 连败，再按复盘重启流程由用户确认后才可考虑重回实盘。")
    elif l2["judged_triggered"]:
        out.append("⛔ 二级熔断判据本次**新触发**（P(g>0) 跌破 80% 且最近连续 "
                   f"{l2['recent_winless_required']} 笔无盈利）——按规则应立即熔断实盘："
                   f"明天起实盘不开新仓，只做模拟盘，等重启判据达标。"
                   f"（执行：`python3 scripts/circuit_breaker.py trip --reason \"...\"`）")
    else:
        if l2["pg_pos"] is not None:
            margin = (l2["pg_pos"] - l2["floor"]) * 100
            out.append(f"✅ 二级熔断闸未触发（P(g>0)={l2['pg_pos'] * 100:.1f}%，"
                       f"距 {l2['floor'] * 100:.0f}% 线 {margin:+.1f}pp；最近 "
                       f"{len(l2['recent_r'])} 笔中盈利 {sum(1 for x in l2['recent_r'] if x > 0)} 笔）")
    if l1["triggered"]:
        out.append(f"⚠️ 一级连败闸今日已触发（{g['market'].upper()} 当日连败 {l1['streak']} 笔）"
                   f"——实盘当日不得再开新仓（模拟盘不受限）；次日重新计数、自动放行。")
    else:
        out.append(f"✅ 一级连败闸未触发（{g['market'].upper()} 当日连败 {l1['streak']} 笔）")
    return out


def build_report(g, stats_today, stats_all):
    l1, l2 = g["L1"], g["L2"]
    pg_txt = "—" if l2["pg_pos"] is None else f"{l2['pg_pos'] * 100:.1f}%"
    l2_verdict = ("⛔ 熔断中" if l2["status"] == "tripped"
                  else ("⛔ 判据触发" if l2["judged_triggered"] else "✅ 未触发"))
    l1_verdict = "⚠️ 已触发" if l1["triggered"] else "✅ 未触发"
    L = [f"# {g['date']} {g['market'].upper()} 自动复盘（停盯收尾生成）", "",
         f"> 由 `auto_review.py` 在停盯收尾时自动生成；样本 = 该市场累积真实样本"
         f"（影子样本不计）。两闸判据与 `trade_utils_tiger` / `circuit_breaker` 同源。", ""]

    L += ["## 一、当日成交（账号口径：含并行会话）", ""]
    if stats_today and stats_today["N"]:
        s = stats_today
        L += ["| 指标 | 值 |", "|---|---|",
              f"| 笔数 | {s['N']} |",
              f"| 胜率 | {s['p'] * 100:.0f}%（{len(s['W'])} 胜 / {len(s['L'])} 败）|",
              f"| 合计净 R | {sum(s['R']):+.2f}R |",
              f"| EV（均值净 R） | {s['EV']:+.3f}R |"]
        if s["N"] > 1:
            L.append(f"| R 标准差 | {s['s']:.3f} |")
    else:
        L.append("本场 0 笔成交。")
    L.append("")

    # —— 样本分布变更点（2026-11-23 立，T209HKUS）：每次自动复盘都亮出来 ——
    _tags = CT.load_tags()
    if _tags:
        _idx = CT.period_index(g["date"], _tags)
        L += ["## 一·bis、样本分布变更点（change tag）", "",
              "> 重大改动会改变「哪些单会成交 / R 怎么算 / 仓位多大 / 漏单多少」。"
              "跨这些点的统计不能直接合并比较——做长期复盘前先看分期间对比"
              "（`review.py` 会打印分期间表）。", "",
              "| 日期 | 改动 | 影响 | 范围 |", "|---|---|---|---|"]
        for t in _tags:
            flag = "" if t.get("verified", True) else "（待确认）"
            L.append(f"| {t['date']} | {t.get('title', '')}{flag} "
                     f"| {CT.affects_label(t.get('affects'))} "
                     f"| {'/'.join(t.get('scope') or [])} |")
        L += ["", f"**本场（{g['date']}）属于期间：{CT.period_label(_idx, _tags)}**"
                  f"——与该期间之前的样本不同质，别直接合并下结论。", ""]

    L += ["## 二、累积样本", ""]
    if stats_all and stats_all["N"]:
        s = stats_all
        L += ["| 指标 | 值 |", "|---|---|",
              f"| 样本量 N | {s['N']} |",
              f"| 胜率 | {s['p'] * 100:.1f}% |",
              f"| EV（均值净 R） | {s['EV']:+.3f}R |",
              f"| 合计净 R | {sum(s['R']):+.2f}R |"]
        if s["N"] > 1:
            L.append(f"| R 标准差 | {s['s']:.3f} |")
    else:
        L.append("累积样本不足。")
    L.append("")

    L += ["## 三、实盘两闸状态", "",
          "| 闸 | 判据 | 当前值 | 结论 |", "|---|---|---|---|",
          f"| 一级 · 连败闸 | 单市场当日连败 ≥3，或 ≥2 且两笔亏损均 ≤ −0.95R | "
          f"{g['market'].upper()} 当日连败 {l1['streak']} 笔 {l1['recent_r']} | {l1_verdict} |",
          f"| 二级 · 熔断闸 | P(g>0)@f 跌破 {l2['floor'] * 100:.0f}% **且** 最近连续 "
          f"{l2['recent_winless_required']} 笔无盈利 | P={pg_txt}，N={l2['n_trades']}；"
          f"最近 {len(l2['recent_r'])} 笔 {l2['recent_r']} | {l2_verdict} |",
          "",
          f"两闸作用域：{l1['scope']}；{l2['scope']}。", ""]

    if l2["status"] == "tripped":
        L += ["### 熔断详情", "",
              f"- 触发时刻：{l2.get('tripped_at')}",
              f"- 触发原因：{l2.get('reason')}", ""]

    L += ["## 四、处置建议", ""] + [f"- {x}" for x in advice(g)] + [""]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="us", choices=["us", "hk"])
    ap.add_argument("--date", default=None, help="该市场交易日 YYYY-MM-DD（缺省=今天）")
    ap.add_argument("--account", default="paper", choices=["paper", "live"])
    ap.add_argument("--no-csv", action="store_true", help="不补样本库 CSV")
    ap.add_argument("--json-only", action="store_true", help="只输出两闸 JSON（不写报告）")
    a = ap.parse_args()

    # 整段「读累积样本 → 合并 → 写样本 → 写报告」在跨进程锁内（多会话同时停盯时串行化）
    with _ReviewLock(f"auto_review_{a.market}.lock"):
        return _run(a)


def _run(a):
    date = a.date or T.market_date_of(a.market)
    src = latest_csv(a.market)
    _excl = load_excludes()
    rows = [r for r in read_rows(src) if not is_excluded(r, _excl)]
    _dropped = 0
    _raw_today = daily_rows(a.market, date, a.account, sec_type_map(rows))
    today_rows = []
    for _r in _raw_today:
        if is_excluded(_r, _excl):
            _dropped += 1
            continue
        today_rows.append(_r)
    if _dropped:
        print(f"🚫 异常样本已排除 {_dropped} 笔（见 reviews/{EXCLUDE_FILE}）：不进累积样本与两闸判据")

    merged = list(rows)
    seen = {row_key(r) for r in rows}
    added = 0
    for r in today_rows:
        if row_key(r) not in seen:
            merged.append(r)
            seen.add(row_key(r))
            added += 1

    csv_path = ROOT / "reviews" / f"{date}-trades-{a.market}.csv"
    if added and not a.no_csv:
        write_csv(csv_path, merged)

    # 统计（review.py 的口径：净 R）
    stats_all = None
    stats_today = None
    try:
        trades_all = [t for t in RV.load_trades(str(csv_path)) if not t.get("shadow")]
        stats_all = RV.summarize(trades_all) if trades_all else None
        trades_today = [t for t in trades_all if str(t.get("date")) == date]
        stats_today = RV.summarize(trades_today) if trades_today else None
    except Exception as e:
        print(f"⚠️ 统计失败（{e}）——报告将只含两闸状态")

    g = gates_status(a.market, date, merged)

    if not a.json_only:
        rep = build_report(g, stats_today, stats_all)
        rep_path = ROOT / "reviews" / f"{date}-review-{a.market}.md"
        rep_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(rep_path, rep)
        print(f"✅ 复盘报告已落盘：{rep_path.relative_to(ROOT)}")
        print(f"✅ 样本库：{csv_path.relative_to(ROOT)}（本次新增 {added} 笔，累计 {len(merged)} 笔）")
        for line in advice(g):
            print(f"  {line}")

    out = {
        "market": a.market,
        "date": date,
        "report": str((ROOT / 'reviews' / f'{date}-review-{a.market}.md').relative_to(ROOT)),
        "csv_rows_added": added,
        "gates": g,
        "advice": advice(g),
        "today": None if not stats_today else {
            "N": stats_today["N"], "win_pct": round(stats_today["p"] * 100, 1),
            "sum_R": round(sum(stats_today["R"]), 3), "EV": round(stats_today["EV"], 3)},
        "all": None if not stats_all else {
            "N": stats_all["N"], "win_pct": round(stats_all["p"] * 100, 1),
            "EV": round(stats_all["EV"], 3)},
    }
    print("JSON=" + json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
