#!/usr/bin/env python3
"""auto 模式独立权益账本（2026-11-18 用户立：auto 与 signal 完全解耦、文件不复用）。

## 为什么单独一套

- **signal 模式**：不连真实账户，权益靠 `signals/equity-log.csv` 由 AI 手动累加。
- **auto 模式**：连券商账户，权益应当**随已平仓交易自动联动**（权益涨 → B 放大、权益跌 → B 缩）。
  2026-11-18 前 auto 借读 signal 的 equity-log（只读不写），导致「复利 / 缩水」机制在 auto 下
  实际是停摆的——每笔盈亏都不回流，B 永远不变。用户明确要求：两模式完全独立、文件不复用。

## 机制（自愈 + 幂等）

```
equity_hkd = baseline_equity_hkd + Σ 已入账平仓交易的净盈亏（折 HKD）
```

- **数据源** = 券商模拟账户的成交单：按父/子单配对（复用 `stop_summary.build_trades`），
  已平仓交易的净盈亏取平仓腿的 `realized_pnl`（与 App 已实现盈亏同源）。
- **幂等**：按平仓腿 order id 去重（`applied` 字典），重复 sync 不会重复累加。
- **自愈**：被动止损 / 止盈触发的平仓、会话中断、脚本漏记——都不影响：下次 sync 自动补齐。
  每次都从 `baseline + Σ applied` 重算（不靠增量），因此不存在长期漂移。
- **币种**：港股交易盈亏为 HKD；美股为 USD，按 `fx`（模拟盘两币种净值比，查不到回退 7.84）折 HKD。
  单一账户、单一权益链（港美同账户）。

## 与旧机制的关系

- `signals/equity-log.csv` 从此**只服务 signal 模式**，auto 不再读它。
- `state/auto_equity.json` 是 auto 的唯一权益权威；`state/` 在 worktree 里是软链（共享持久状态），
  所以 worktree 盯盘也读得到同一份账本（旧机制在 worktree 读不到 equity-log 会退回 initial_equity，
  这个坑随之消失）。

## 用法

```bash
python3 auto_equity.py show              # 看当前账本（会先 sync 一次）
python3 auto_equity.py sync              # 强制同步并打印新增记录
python3 auto_equity.py reset --baseline 100000   # 重设基准（用户决定，AI 不擅自调用）
```

被 `trade_utils_tiger.paper_sizing_reference()` / `load_equity(mode='auto')` 调用，
因此开仓脚本、preflight、resume、monitor 的 equity 口径全部走这里。
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = _SCRIPT_DIR.parent.parent.parent.parent
STATE_DIR = PROJECT_ROOT / "state"
LEDGER = STATE_DIR / "auto_equity.json"
CONFIG_PATH = _SCRIPT_DIR.parent / "config.json"   # .pi/skills/trade/config.json

DEFAULT_FX = 7.84          # 查不到模拟盘两币种净值比时的保守兜底
_EPS_TIME_BUFFER = timedelta(days=1)   # 查询窗口向前多留 1 天（跨日 / 时区缓冲）

# 同一进程内短缓存：open_position 会在 auto_sizing_equity / 购买力上限两处各调一次，
# 每次 sync 要拉港美两份订单 + 汇率（≈3 次 API 往返），不缓存会白多等 2-4 秒。
# 60 秒 TTL 足够覆盖单次开仓调用，又不会跨“平仓 → 下一笔开仓”的进程边界（那是新进程、缓存已空）。
_CACHE = {"ts": 0.0, "info": None}
_CACHE_TTL = 60.0


def _load_config_risk():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f).get("risk", {}) or {}
    except Exception:
        return {}


def _baseline_from_config():
    risk = _load_config_risk()
    for key in ("auto_initial_equity", "initial_equity"):
        v = risk.get(key)
        if v:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 100000.0


def _now():
    return datetime.now(timezone.utc)


def load_ledger():
    """读账本；不存在则按 config 建一份（baseline = auto_initial_equity / initial_equity）。"""
    if LEDGER.exists():
        try:
            with open(LEDGER, encoding="utf-8") as f:
                d = json.load(f)
            if "baseline_equity_hkd" in d and "applied" in d:
                return d
        except Exception:
            pass    # 文件损坏 → 重建（保留 .bak 供排查）
        try:
            os.replace(LEDGER, str(LEDGER) + ".bak")
        except Exception:
            pass
    d = {
        "baseline_equity_hkd": _baseline_from_config(),
        "created": _now().isoformat(timespec="seconds"),
        "applied": {},
        "equity_hkd": _baseline_from_config(),
        "last_sync": None,
        "note": "auto 模式独立权益账本（2026-11-18 用户立；与 signal 的 signals/equity-log.csv 完全解耦）",
    }
    _save(d)
    return d


def _save(d):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = str(LEDGER) + ".tmp"
    real = os.path.realpath(LEDGER) if os.path.exists(LEDGER) else None
    target = real or str(LEDGER)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, target)


def _fx_hkd_per_usd():
    """模拟盘两币种净值比（不碰实盘）；查不到回退 DEFAULT_FX。"""
    try:
        sys.path.insert(0, str(_SCRIPT_DIR))
        import trade_utils_tiger as T
        cfg = T.load_config()
        tc = T.new_trade_client(cfg)
        # 口径与 trade_utils_tiger.paper_sizing_reference 一致：取两次不同 base_currency 的
        # prime_assets，用各自 segments 的 net_liquidation 相比 → HKD/USD 汇率
        nl = {}
        for cur in ("HKD", "USD"):
            pa = tc.get_prime_assets(base_currency=cur)
            for seg in (getattr(pa, "segments", None) or {}).values():
                v = getattr(seg, "net_liquidation", None)
                if v:
                    nl[cur] = float(v)
        if nl.get("HKD") and nl.get("USD"):
            return nl["HKD"] / nl["USD"]
    except Exception:
        pass
    return DEFAULT_FX


def _fetch_closed_trades(since_dt):
    """券商成交单 → 已平仓交易列表（[(market, trade_dict)]）。复用 stop_summary 的配对逻辑。"""
    sys.path.insert(0, str(_SCRIPT_DIR))
    import stop_summary as SS
    end = _now() + timedelta(minutes=5)
    start = since_dt - _EPS_TIME_BUFFER
    out = []
    for market in ("hk", "us"):
        try:
            orders, _masked = SS.fetch_orders(market, "paper", start, end)
            for t in SS.build_trades(orders):
                if t.get("exit") is None:      # 未平仓（无平仓腿成交）
                    continue
                out.append((market, t))
        except Exception as e:
            raise RuntimeError(f"拉取 {market.upper()} 订单失败：{e}") from e
    return out


def sync(verbose=False, force=False):
    """同步账本并返回当前 auto equity。永不抛异常（失败时返回上次已知值 + degraded 说明）。"""
    import time as _t
    if (not force) and _CACHE["info"] is not None and (_t.time() - _CACHE["ts"]) < _CACHE_TTL:
        info = dict(_CACHE["info"])
        info["cached"] = True
        if verbose:
            print(json.dumps(info, ensure_ascii=False, indent=2))
        return info
    led = load_ledger()
    info = {
        "equity_hkd": float(led.get("equity_hkd") or led["baseline_equity_hkd"]),
        "baseline_equity_hkd": float(led["baseline_equity_hkd"]),
        "newly": [],
        "applied_count": len(led.get("applied") or {}),
        "source": "",
        "degraded": [],
    }
    try:
        created = datetime.fromisoformat(led["created"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        fx = _fx_hkd_per_usd()
        trades = _fetch_closed_trades(created)
        created_ms = created.timestamp() * 1000
        changed = False
        for market, t in trades:
            # 只认「账本建立之后才平仓」的交易：查询窗口向前多留 1 天是为了兜住跨日持仓，
            # 但基准权益是建立那一刻的 10 万，之前的平仓不能算进来（否则基准被历史盈亏污染）。
            if not t.get("exit_time") or float(t["exit_time"]) < created_ms:
                continue
            oid = str(t.get("exit_order_id") or "")
            if not oid or oid in (led["applied"] or {}):
                continue
            pnl = float(t.get("pnl") or 0.0)
            pnl_hkd = pnl * fx if market == "us" else pnl
            led["applied"][oid] = {
                "market": market,
                "symbol": t.get("symbol"),
                "direction": t.get("direction"),
                "qty": t.get("qty"),
                "exit": t.get("exit"),
                "pnl": round(pnl, 2),
                "pnl_hkd": round(pnl_hkd, 2),
                "ts": (datetime.fromtimestamp((t.get("exit_time") or 0) / 1000, tz=timezone.utc)
                       .isoformat(timespec="seconds") if t.get("exit_time") else None),
            }
            info["newly"].append(dict(led["applied"][oid]))
            changed = True
        total = sum(v.get("pnl_hkd", 0.0) for v in led["applied"].values())
        led["equity_hkd"] = round(float(led["baseline_equity_hkd"]) + total, 2)
        led["last_sync"] = _now().isoformat(timespec="seconds")
        led["last_fx"] = round(fx, 4)
        info["equity_hkd"] = led["equity_hkd"]
        info["applied_count"] = len(led["applied"])
        if changed or not LEDGER.exists():
            _save(led)
        else:
            _save(led)   # 更新时间戳，便于排查（写入很轻）
        info["source"] = (
            f"state/auto_equity.json（auto 独立账本：基准 {info['baseline_equity_hkd']:,.0f} HKD + "
            f"已入账 {info['applied_count']} 笔实现盈亏；汇率 {fx:.4f}）")
    except Exception as e:
        info["degraded"].append(f"同步失败，沿用上次已知值：{e}")
        info["source"] = (f"state/auto_equity.json（⚠️ 本次同步失败，沿用上次值；"
                          f"已入账 {info['applied_count']} 笔）")
    if verbose:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    info["cached"] = False
    _CACHE["ts"] = _t.time()
    _CACHE["info"] = dict(info)
    return info


def main():
    import argparse
    ap = argparse.ArgumentParser(description="auto 模式独立权益账本")
    ap.add_argument("cmd", choices=("show", "sync", "reset"))
    ap.add_argument("--baseline", type=float, help="reset 时的新基准（HKD）")
    args = ap.parse_args()
    if args.cmd == "show":
        info = sync(force=True)
        led = load_ledger()
        print(f"auto equity = {info['equity_hkd']:,.2f} HKD"
              f"（基准 {info['baseline_equity_hkd']:,.2f} + 已入账 {info['applied_count']} 笔）")
        print(f"来源：{info['source']}")
        if info["degraded"]:
            for d in info["degraded"]:
                print(f"⚠️ {d}")
        for oid, v in (led.get("applied") or {}).items():
            print(f"  · {v.get('ts')} {str(v.get('symbol')):12} {v.get('direction','') or '':5} "
                  f"{v.get('pnl_hkd'):+10.2f} HKD  (order {oid})")
    elif args.cmd == "sync":
        info = sync(verbose=True, force=True)
        print(f"\n同步完成：equity = {info['equity_hkd']:,.2f} HKD，新增 {len(info['newly'])} 笔")
    else:   # reset
        if args.baseline is None:
            sys.exit("reset 需要 --baseline <HKD 金额>")
        old = load_ledger()
        bak = str(LEDGER) + f".reset-{_now().strftime('%Y%m%dT%H%M%S')}"
        if LEDGER.exists():
            os.replace(os.path.realpath(LEDGER), bak)
        new = {
            "baseline_equity_hkd": float(args.baseline),
            "created": _now().isoformat(timespec="seconds"),
            "applied": {},
            "equity_hkd": float(args.baseline),
            "last_sync": None,
            "note": f"用户重设基准（旧账本已备份到 {os.path.basename(bak)}）",
        }
        _save(new)
        print(f"已重设 auto 基准：{args.baseline:,.2f} HKD（旧账本备份：{bak}）")


if __name__ == "__main__":
    main()
