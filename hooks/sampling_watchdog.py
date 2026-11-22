#!/usr/bin/env python3
"""盯盘中断外部看护（2026-11-22 立，T202HKUS）。

为什么需要它（2026-11-20 实录）：美股会话在 ET 11:03 采样链断掉之后，**再没有发生任何回合**
（没有后台任务完成通知、AI 不会自己醒来）。而 `monitor_guard.py` 的断链兜底挂在
`agent_settled`（回合静止）上——没有回合就没有触发点，那个兜底再准也发不出提醒。
结果是整个后半场无人盯盘、约 5 小时后才被用户发现，停盯收尾迟到约 24 小时。

所以补一层**完全独立于 AI 会话**的看护（launchd 每 60 秒跑一次，AI 死了它照样跑），
每次只看三件事实：

  ① 现在是不是本市场的交易窗口——直接复用 `monitor_guard` 的 `in_hk_session` /
     `in_us_session`（同一套窗口口径，不另写一份，避免两处漂移）；
  ② 本市场今天有没有盯过盘——存在本市场、本场日期的 `monitor_log` 才算（没有就是今天
     没盯这个市场，空仓日 / 盘外 / 只盯另一个市场一律静默）；
  ③ 采样链是不是真断了——段进程不在（`sampling_process_alive`）+ 本市场最后一份 log
     已超过 `GAP_ALERT_SECONDS`（默认 300 秒；正常段间空窗只有几秒）。

三条同时成立 = 「盘中该盯、并且今天在盯、却断了」，此时做两件事：

  - **唤醒会话**：从 `tmp/monitor_market_*.json`（采样脚本段启动时写的会话→市场映射）
    找出盯这个市场的会话，用 `pi-intercom send` 直接给它发一条消息——消息进来就会触发
    一个回合，AI 于是能看到「链断了」并重启采样段；
  - **发飞书**：会话已死 / 不在线 / intercom 不通时的兜底，用户在手机上就能看到。

节流与出口：同一市场 `COOLDOWN_SECONDS`（10 分钟）内只告警一次、每个交易日最多
`MAX_ALERTS_PER_DAY`（6 次）；已正常停盯（`state/stop_done_<市场>_<交易日>*.txt` 且该标记
比最后一份 log 更新）一律静默。任何异常都静默退出（看护自己不能变成新的故障源）。

部署：`hooks/com.daytrading.sampling-watchdog.plist`（launchd 每 60 秒），
由 `preflight.py` 的 `_ensure_watcher()` 在每次盯盘启动时自动部署 / 自愈；也可手动
`bash hooks/install_sampling_watchdog.sh`。心跳写 `state/sampling_watchdog_beat.json`
（preflight 据此判断「脚本真的被执行过」，而不是只注册在 launchctl 里）。

用法：
    python3 hooks/sampling_watchdog.py               # 定时巡检（launchd 调用）
    python3 hooks/sampling_watchdog.py --status      # 打印当前判定与节流状态（不发任何消息）
    python3 hooks/sampling_watchdog.py --dry-run     # 只判定、只校验文案，不发送、不写状态
    python3 hooks/sampling_watchdog.py --force       # 忽略窗口 / 节流，立即按当前事实判定并发一条
    python3 hooks/sampling_watchdog.py --now "2026-11-23 23:00:00" --status   # 指定本机时间（测试）
"""

import argparse
import glob
import json
import os
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import monitor_guard as mg  # noqa: E402  （同目录守卫，窗口 / 日志目录 / 进程判定单一源）

PROJECT_ROOT = mg._PROJECT_ROOT
STATE_DIR = mg.STATE_DIR
NOTIFY = os.path.join(PROJECT_ROOT, ".pi", "skills", "trade", "scripts", "notify_feishu.py")
INTERCOM = os.path.expanduser("~/.pi/agent/npm/node_modules/.bin/pi-intercom")

GAP_ALERT_SECONDS = 300      # 本市场最后一份 log 超过这个时长 = 断了（正常段间空窗 <90 秒）
COOLDOWN_SECONDS = 600       # 同一市场的两次告警最小间隔
MAX_ALERTS_PER_DAY = 6       # 每个交易日每个市场的告警上限（会话真死时避免整夜刷屏）
STATE_PATH = os.path.join(STATE_DIR, "sampling_watchdog.json")
BEAT_PATH = os.path.join(STATE_DIR, "sampling_watchdog_beat.json")


# —— 基础工具 ——

def now_local(explicit=None):
    if explicit:
        return datetime.strptime(explicit, "%Y-%m-%d %H:%M:%S")
    return datetime.now()


def write_beat(now):
    """心跳：preflight 用它验证「脚本真的跑过」，而不是仅仅注册在 launchctl 里。"""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = BEAT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"updated": now.strftime("%Y-%m-%d %H:%M:%S")}, fh, ensure_ascii=False)
        os.replace(tmp, BEAT_PATH)
    except OSError:
        pass


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


# —— 事实判定 ——

def market_log_stats(market, now):
    """本市场「本场」的采样日志统计：返回 (是否存在, 最新一份距今秒数)。

    「本场」= 文件名日期落在 `_active_log_dates()`（北京 + 美东双日历）里，或最近 12 小时
    内更新过（兜住命名口径分歧）。只看本市场前缀的日志——否则「上午盯港股、晚上盯美股」
    的场景里，早上那份港股的旧 log 会让美股的断链判定失真。
    """
    dates = mg._active_log_dates(now)
    recent_cut = now.timestamp() - 12 * 3600
    prefix = f"monitor_log_{market}_"
    newest = None
    for d in mg._log_dirs():
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for f in names:
            if not (f.startswith(prefix) and f.endswith(".csv")):
                continue
            p = os.path.join(d, f)
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            if not (any(f"_{x}_" in f for x in dates) or mtime >= recent_cut):
                continue
            newest = mtime if newest is None else max(newest, mtime)
    if newest is None:
        return False, None
    return True, now.timestamp() - newest


def market_date_of(market, now):
    """把 `now` 换算成该市场的交易日 'YYYY-MM-DD'。

    必须用传入的 now 而不是 `monitor_guard._market_today()`（那个函数内部取真实时钟，
    在测试注入假时钟时会算错日期——2026-11-22 实测：停盯标记晚于最后日志却仍报警）。
    """
    if market == "US":
        try:
            from zoneinfo import ZoneInfo
            return now.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        except Exception:
            pass
    return now.strftime("%Y-%m-%d")


def stop_done_after_last_log(market, now):
    """本市场是否「已经正常停盯」：有 stop_done 标记，且标记比最后一份日志更新。

    只认 market 口径的 `stop_done_<市场>_<交易日>*.txt`（由 stop_summary / mark_stop_done 写）。
    多会话场景下，别人先做完收尾、这一场还在盯时——标记会早于后来的日志，此时不抑制。
    """
    market_date = market_date_of(market, now)
    markers = glob.glob(os.path.join(STATE_DIR, f"stop_done_{market.lower()}_{market_date}*.txt"))
    if not markers:
        return False
    newest_marker = max(os.path.getmtime(p) for p in markers)
    _, age = market_log_stats(market, now)
    if age is None:
        return True
    last_log_ts = now.timestamp() - age
    return newest_marker >= last_log_ts


def owner_sessions(market, now):
    """盯这个市场的会话（按 `tmp/monitor_market_*.json` 的 updated 从新到旧）。"""
    out = []
    recent_cut = now.timestamp() - 12 * 3600
    for d in mg._log_dirs():
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for f in names:
            if not (f.startswith("monitor_market_") and f.endswith(".json")):
                continue
            p = os.path.join(d, f)
            try:
                with open(p, encoding="utf-8") as fh:
                    rec = json.load(fh)
                updated = float(rec.get("updated") or os.path.getmtime(p))
            except (OSError, ValueError):
                continue
            if rec.get("market") != market or updated < recent_cut:
                continue
            stag = str(rec.get("session_tag") or f[len("monitor_market_"):-len(".json")])
            if len(stag) == 32:
                sid = f"{stag[:8]}-{stag[8:12]}-{stag[12:16]}-{stag[16:20]}-{stag[20:]}"
            else:
                sid = stag
            out.append((updated, sid, stag))
    out.sort(reverse=True)
    return out


def allow_alert(market, now, state):
    """节流：同市场 10 分钟一次，每交易日最多 6 次。返回 (可发, 原因)。"""
    day = market_date_of(market, now)
    cell = (state.get("alerts") or {}).get(market) or {}
    if cell.get("date") != day:
        return True, "本交易日首次"
    count = int(cell.get("count") or 0)
    if count >= MAX_ALERTS_PER_DAY:
        return False, f"本交易日已告警 {count} 次（上限 {MAX_ALERTS_PER_DAY}）"
    last = float(cell.get("last_ts") or 0)
    if now.timestamp() - last < COOLDOWN_SECONDS:
        return False, f"距上次告警不足 {COOLDOWN_SECONDS // 60} 分钟"
    return True, "冷却已过"


def record_alert(market, now, state):
    day = market_date_of(market, now)
    alerts = state.setdefault("alerts", {})
    cell = alerts.get(market) or {}
    if cell.get("date") != day:
        cell = {"date": day, "count": 0, "last_ts": 0}
    cell["count"] = int(cell.get("count") or 0) + 1
    cell["last_ts"] = now.timestamp()
    cell["last_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    alerts[market] = cell
    save_state(state)


# —— 对外动作 ——

def feishu_text(market, gap_sec, now):
    label = "美股" if market == "US" else "港股"
    et = now.date()
    gap_min = max(1, int(gap_sec // 60))
    try:
        from zoneinfo import ZoneInfo
        et = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        pass
    last_et = (now.timestamp() - gap_sec)
    try:
        from datetime import datetime as _dt
        last_et_txt = _dt.fromtimestamp(last_et).astimezone(ZoneInfo("America/New_York")).strftime("%m-%d %H:%M")
    except Exception:
        last_et_txt = "未记录"
    return (
        f"🚨 **盯盘中断提醒（{label}）**\n\n"
        f"这一场盯盘已经 **{gap_min} 分钟**没有新的行情数据"
        f"（最后一条：美东 {last_et_txt}）。\n\n"
        f"如果不是计划内的中断（例如最后一段跑完没接上），请回到盯盘窗口恢复；"
        f"如果是正常收尾，请确认停盯总结已经发出。\n\n"
        f"（外部看护自动提醒；同一市场 {COOLDOWN_SECONDS // 60} 分钟内不重复，"
        f"每场最多 {MAX_ALERTS_PER_DAY} 次。）"
    )


def intercom_text(market, gap_sec, now):
    label = "美股" if market == "US" else "港股"
    gap_min = max(1, int(gap_sec // 60))
    return (
        f"⚠️ 外部看护（sampling_watchdog）｜{label}盘中密采样已停 {gap_min} 分钟："
        f"本场最后一份 monitor_log 是 {gap_min} 分钟前，且段进程不在。\n"
        f"这是「上一段跑完没起下一段」的形态，而会话已没有回合、`agent_settled` 兜底发不出来，"
        f"所以才由外部直接叫你。\n"
        f"请立刻 background_run 起下一段密采样（标的与关键位按本场最后一份 actions 记录 / "
        f"最后一份 monitor_log 的文件名恢复）；若确已到停盯边界，走停盯收尾"
        f"（stop_summary → keepawake_off → monitor_unregister）。"
    )


def send_feishu(market, gap_sec, now, dry_run=False):
    if not os.path.isfile(NOTIFY):
        return False, f"找不到飞书发送脚本：{NOTIFY}"
    cmd = [sys.executable, NOTIFY, "--text", feishu_text(market, gap_sec, now),
           "--market", market.lower(), "--no-mark", "--no-session-id"]
    if dry_run:
        cmd.append("--dry-run")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except Exception as exc:
        return False, f"调用飞书发送脚本异常：{exc}"
    out = (res.stdout or "") + (res.stderr or "")
    if res.returncode != 0:
        return False, out.strip()[:300]
    return True, out.strip()[:200]


def live_sessions():
    """在线会话 {session_id: {name, cwd}}（pi-intercom list --json）。

    为什么必须查：pi-intercom 的 `--to <id>` 在目标不在线时会**退回到「唯一的在线会话」**
    （2026-11-22 实测：传一个不存在的 id，消息被投递给另一个会话）。看护的消息内容是
    「盘中断链、立刻起采样段」，投错会话等于对无关的 agent 谎报军情。所以只对**确认在线、
    且 cwd 在本项目（主工作区或本项目 worktree）**的会话发送。
    """
    try:
        res = subprocess.run([INTERCOM, "list", "--json"], capture_output=True,
                             text=True, timeout=20)
        data = json.loads(res.stdout or "{}")
        out = {}
        for sess in (data.get("sessions") or []):
            sid = str(sess.get("id") or "")
            if sid:
                out[sid] = {"name": sess.get("name"), "cwd": str(sess.get("cwd") or "")}
        return out
    except Exception:
        return {}


def _is_project_session(cwd):
    """会话 cwd 是否属于本项目：主工作区 `trading-agent` 或它的 worktree `trading-agent-*`。"""
    if not cwd:
        return False
    root = PROJECT_ROOT.rstrip(os.sep)
    prefixes = (root + os.sep, root + "-")
    return cwd == root or cwd.startswith(prefixes)


def wake_sessions(market, gap_sec, now, dry_run=False):
    """给盯这个市场的会话发 intercom 消息；返回 [(sid, ok, detail)]。"""
    results = []
    if not os.path.isfile(INTERCOM):
        return results
    candidates = owner_sessions(market, now)[:2]
    if dry_run:
        return [(sid, True, "dry-run 未发送") for _u, sid, _s in candidates]
    live = live_sessions()
    if not live:
        return [(sid, False, "intercom 无在线会话（只用飞书兜底）") for _u, sid, _s in candidates]
    for _updated, sid, _stag in candidates:
        info = live.get(sid)
        if not info:
            results.append((sid, False, "该会话不在线——跳过，避免误唤醒别的会话"))
            continue
        if not _is_project_session(info.get("cwd") or ""):
            results.append((sid, False, f"会话 cwd 不在本项目（{info.get('cwd')}）——跳过"))
            continue
        try:
            res = subprocess.run(
                [INTERCOM, "send", "--to", sid, "--text", intercom_text(market, gap_sec, now)],
                capture_output=True, text=True, timeout=30,
            )
            results.append((sid, res.returncode == 0,
                            ((res.stdout or "") + (res.stderr or "")).strip()[:160]))
        except Exception as exc:
            results.append((sid, False, f"调用失败：{exc}"))
    return results


# —— 主流程 ——

def describe(market, now):
    """给 --status / 日志用的一行判定说明。"""
    exists, age = market_log_stats(market, now)
    alive, script = mg.sampling_process_alive()
    if not exists:
        return f"{market}：本场没有采样日志 → 今天没盯这个市场，静默"
    if alive:
        return f"{market}：段进程在跑（{script}）→ 正常"
    if age is not None and age < GAP_ALERT_SECONDS:
        return f"{market}：最后一份日志 {int(age)} 秒前（< {GAP_ALERT_SECONDS} 秒宽限）→ 正常"
    if stop_done_after_last_log(market, now):
        return f"{market}：已正常停盯（停盯标记晚于最后一份日志）→ 静默"
    return (f"{market}：段进程不在、最后一份日志 "
            f"{'无' if age is None else str(int(age)) + ' 秒前'} → 中断，应告警")


def main():
    ap = argparse.ArgumentParser(description="盯盘中断外部看护（独立于 AI 会话）")
    ap.add_argument("--status", action="store_true", help="只打印判定与节流状态，不发消息")
    ap.add_argument("--dry-run", action="store_true", help="只判定并校验文案，不发送、不写状态")
    ap.add_argument("--force", action="store_true", help="忽略窗口与节流，按当前事实立即判定")
    ap.add_argument("--now", help="指定本机时间 'YYYY-MM-DD HH:MM:SS'（测试用）")
    args = ap.parse_args()

    now = now_local(args.now)
    if not args.dry_run:
        write_beat(now)

    market = None
    if mg.in_hk_session(now):
        market = "HK"
    elif mg.in_us_session(now):
        market = "US"

    if args.status:
        print(f"现在：{now.strftime('%Y-%m-%d %H:%M:%S')}｜窗口内市场：{market or '无'}")
        for mk in ("HK", "US"):
            print("  " + describe(mk, now))
        state = load_state()
        print(f"  节流状态：{json.dumps(state.get('alerts') or {}, ensure_ascii=False)}")
        return 0

    if market is None and not args.force:
        return 0

    market = market or ("US" if not mg.in_hk_session(now) else "HK")
    exists, age = market_log_stats(market, now)
    if not exists:
        return 0
    alive, _script = mg.sampling_process_alive()
    if alive:
        return 0
    if age is not None and age < GAP_ALERT_SECONDS:
        return 0
    if stop_done_after_last_log(market, now):
        return 0

    state = load_state()
    if not args.force:
        ok, why = allow_alert(market, now, state)
        if not ok:
            return 0
    else:
        why = "force"

    woke = wake_sessions(market, age or 0, now, dry_run=args.dry_run)
    ok_feishu, detail = send_feishu(market, age or 0, now, dry_run=args.dry_run)

    if not args.dry_run:
        record_alert(market, now, state)
    print(f"[sampling-watchdog] {now.strftime('%Y-%m-%d %H:%M:%S')}｜{market}｜"
          f"断 {int(age or 0)} 秒｜触发：{why}｜"
          f"飞书 {'✅' if ok_feishu else '❌'} {detail[:120]}｜"
          f"唤醒 {woke if woke else '（无可用会话映射）'}")
    return 0 if (ok_feishu or woke) else 1


if __name__ == "__main__":
    sys.exit(main())
