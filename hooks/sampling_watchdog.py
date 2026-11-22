#!/usr/bin/env python3
"""盯盘中断外部看护（2026-11-22 立，T202HKUS；全自动、只用项目自己的机制）。

为什么需要它（2026-11-20 实录）：美股会话在 ET 11:03 采样链断掉之后，**再没有发生任何回合**
（没有后台任务完成通知、AI 不会自己醒来）。而 `monitor_guard.py` 的断链兜底挂在
`agent_settled`（回合静止）上——没有回合就没有触发点，那个兜底再准也发不出提醒。
结果是整个后半场无人盯盘、约 5 小时后才被用户发现，停盯收尾迟到约 24 小时。

**两条硬约束（2026-11-22 用户立）**：
  ① 全自动、不依赖人工介入——不发飞书、不弹窗喊人；
  ② **不用项目外的工具**——所以唤醒通道不放给任何外部 CLI，而是**项目自己的文件信箱 +
     项目自己的 pi 扩展**（见下）。

做两件事，都是项目内部机制：

  ① **投递唤醒请求（文件信箱）**：盘中本场断链时，看护往会话自己的 `tmp/wake_requests/`
     写一个 `<stag>.json`；会话内的扩展 `.pi/extensions/trade-guards.ts` 每 15 秒看一眼
     自己的信箱，有请求就删掉并 `sendMessage(triggerTurn)` 唤醒一轮——AI 于是能重启采样段
     并补上缺的分析。全程只用「项目里的文件 + pi 扩展 API + tmux（项目既定的后台进程载体）」，
     不依赖任何第三方包或服务。
  ② **叫不醒就自己接上**：没有候选会话、或请求投递 5 分钟仍没被认领（会话已死 / 没在跑），
     看护按「本场最后在盯的标的与模式」用 tmux 起一段密采样——数据链不再断。
     接管痕迹写 `tmp/sampling_watchdog_takeover_<市场>.log`，AI 回来后读一眼就知道
     「这段时间是看护代跑的，分析要补」。

判定只看三件事实（每次 60 秒巡检）：

  ① **窗口**：直接复用 `monitor_guard.in_hk_session` / `in_us_session`（同一套窗口口径，
     不另写一份，避免两处漂移）；
  ② **本场在盯**：存在**本市场**、**本场日期**（北京 / 美东双日历）的 `monitor_log`——
     只盯过港股、现在美股盘中的场景不会误判；空仓日 / 盘外静默；
  ③ **真断了**：段进程不在（`sampling_process_alive`）+ 本市场最后一份日志已超过
     `GAP_ALERT_SECONDS`（300 秒；正常段间空窗只有几秒）。

节流：同一市场 10 分钟内最多投一次唤醒请求；投递后给会话 `WAKE_RECLAIM_SECONDS`（300 秒）
认领时间，期间不接管（避免「会话正在重启段」和「看护起段」撞车）；两次接管之间至少隔
`MIN_RESTART_INTERVAL_SECONDS`（120 秒），防止某段启动即失败时每分钟重试。
已正常停盯（`state/stop_done_<市场>_<交易日>*.txt` 晚于最后一份日志）一律静默。
任何异常都静默退出——看护自己不能变成新的故障源。

部署：`hooks/com.daytrading.sampling-watchdog.plist`（launchd 每 60 秒），
由 `preflight.py` 的 `_ensure_watcher()` 在每次盯盘启动时自动部署 / 自愈；也可手动
`bash hooks/install_sampling_watchdog.sh`。心跳写 `state/sampling_watchdog_beat.json`
（preflight 据此判断「脚本真的被执行过」，而不是只注册在 launchctl 里）。

用法：
    python3 hooks/sampling_watchdog.py               # 定时巡检（launchd 调用）
    python3 hooks/sampling_watchdog.py --status      # 打印当前判定与节流状态（不做任何动作）
    python3 hooks/sampling_watchdog.py --dry-run     # 只判定、只打印将要执行的动作，不写信箱、不起进程
    python3 hooks/sampling_watchdog.py --force       # 忽略窗口 / 节流，立即按当前事实判定并执行
    python3 hooks/sampling_watchdog.py --now "2026-11-23 23:00:00" --status   # 指定本机时间（测试）
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import monitor_guard as mg  # noqa: E402  （同目录守卫，窗口 / 日志目录 / 进程判定单一源）

PROJECT_ROOT = mg._PROJECT_ROOT
STATE_DIR = mg.STATE_DIR
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, ".pi", "skills", "trade", "scripts")
SEGMENT_SCRIPT = os.path.join(SCRIPTS_DIR, "monitor_segment.py")

GAP_ALERT_SECONDS = 300            # 本市场最后一份 log 超过这个时长 = 断了（正常段间空窗 <90 秒）
WAKE_COOLDOWN_SECONDS = 600        # 同一市场两次「投递唤醒请求」的最小间隔
WAKE_RECLAIM_SECONDS = 300         # 投递后给会话的认领时间，期间看护不接管（防撞车）
MIN_RESTART_INTERVAL_SECONDS = 120  # 两次「自己接管起段」的最小间隔
MAX_TARGETS = 4                    # 自己接管时最多盯几只（按最后一份日志的标的顺序取）
SEGMENT_SECONDS = 40               # 与 trade skill 的 40 秒密采样一致（脚本内部也夹到 40）
SEGMENT_INTERVAL = 10
WAKE_DIRNAME = "wake_requests"     # 会话自己 tmp 下的信箱目录（扩展每 15 秒看一次）
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


def market_date_of(market, now):
    """把 `now` 换算成该市场的交易日 'YYYY-MM-DD'（用传入的 now，不取真实时钟）。"""
    if market == "US":
        try:
            from zoneinfo import ZoneInfo
            return now.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        except Exception:
            pass
    return now.strftime("%Y-%m-%d")


# —— 事实判定 ——

LOG_RE = re.compile(r"^monitor_log_(HK|US)_([A-Z0-9_]+)_(\d{8})_(signal|auto)\.csv$")


def collect_logs(market, now):
    """本市场「本场」的采样日志：[(mtime, symbol, mode, path)]，按 mtime 新→旧。

    「本场」= 文件名日期落在 `_active_log_dates()`（北京 + 美东双日历）里，或最近 12 小时
    内更新过（兜住命名口径分歧）。只看本市场前缀的日志——否则「上午盯港股、晚上盯美股」
    的场景里，早上那份港股的旧 log 会让美股的断链判定失真。
    """
    dates = mg._active_log_dates(now)
    recent_cut = now.timestamp() - 12 * 3600
    out = []
    for d in mg._log_dirs():
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for f in names:
            m = LOG_RE.match(f)
            if not m or m.group(1) != market:
                continue
            p = os.path.join(d, f)
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            if not (m.group(3) in dates or mtime >= recent_cut):
                continue
            # 文件名里的标的是「点换下划线」形态（US_TQQQ / HK_00700 / US_BRK_B），还原成代码
            out.append((mtime, f"{market}.{m.group(2).replace('_', '.')}", m.group(4), p))
    out.sort(reverse=True)
    return out


def market_log_stats(market, now):
    """返回 (是否存在本场日志, 最新一份距今秒数)。"""
    logs = collect_logs(market, now)
    if not logs:
        return False, None
    return True, now.timestamp() - logs[0][0]


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
    return newest_marker >= now.timestamp() - age


# —— 动作一：往会话信箱投递唤醒请求（项目内机制，不碰外部工具） ——

def market_sessions(market, now):
    """盯这个市场的会话：[(updated, sid, stag, log_dir)]，按 updated 新→旧。

    数据来源是采样脚本段启动时写的 `tmp/monitor_market_*.json`（会话自己的 tmp），
    `log_dir` 就是那个 tmp 目录——唤醒请求要写回**会话自己**的 tmp，扩展才看得到。
    """
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
            out.append((updated, sid, stag, d))
    out.sort(reverse=True)
    return out


def wake_request_path(entry):
    _updated, _sid, stag, log_dir = entry
    return os.path.join(log_dir, WAKE_DIRNAME, f"{stag}.json")


def write_wake_request(entry, market, gap_sec, now, dry_run=False):
    """往会话信箱写一条唤醒请求（原子写；扩展读后删除）。返回 (是否写入, 路径)。"""
    path = wake_request_path(entry)
    if dry_run:
        return True, path
    payload = {
        "market": market,
        "gap_sec": int(gap_sec or 0),
        "at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "from": "sampling_watchdog",
        "session": entry[1],
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
        return True, path
    except OSError as exc:
        return False, f"{path}（{exc}）"


def pending_wake_requests(market, now):
    """尚未被会话认领的请求：[(path, age_sec)]（按年龄从大到小）。"""
    out = []
    for entry in market_sessions(market, now):
        path = wake_request_path(entry)
        try:
            age = now.timestamp() - os.path.getmtime(path)
        except OSError:
            continue
        out.append((path, age))
    out.sort(key=lambda x: -x[1])
    return out


# —— 动作二：自己接管（叫不醒时把链接回去） ——

def takeover_targets(market, now):
    """按本场最后的采样日志推出「接管时该盯什么」：返回 (targets 串, mode) 或 (None, None)。"""
    logs = collect_logs(market, now)
    if not logs:
        return None, None
    mode = logs[0][2]
    seen, syms = set(), []
    for _mtime, sym, _mode, _p in logs:
        if sym in seen:
            continue
        seen.add(sym)
        syms.append(sym)
        if len(syms) >= MAX_TARGETS:
            break
    return ",".join(syms), mode


def takeover_command(market, now):
    """构建接管命令（不执行）：返回 (argv, tmux_session, log_path) 或 (None, None, None)。"""
    targets, mode = takeover_targets(market, now)
    if not targets or not os.path.isfile(SEGMENT_SCRIPT):
        return None, None, None
    argv = [sys.executable, SEGMENT_SCRIPT, targets, str(SEGMENT_SECONDS),
            str(SEGMENT_INTERVAL), "--mode", mode or "signal"]
    return argv, f"pi-sampling-wd-{market.lower()}", takeover_log_path(market)


def takeover_log_path(market):
    return os.path.join(PROJECT_ROOT, "tmp", f"sampling_watchdog_takeover_{market}.log")


def _spawn(argv, tmux_session, log_path, now):
    """用 tmux 起一段（项目规定：后台进程一律在 tmux 里，窗口/终端挂了任务不丢）。"""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    quoted = " ".join(_shell_quote(a) for a in argv)
    cmd = (f"{quoted} >> {_shell_quote(log_path)} 2>&1")
    try:
        subprocess.run(["tmux", "kill-session", "-t", tmux_session],
                       capture_output=True, text=True, timeout=10)
    except Exception:
        pass
    try:
        res = subprocess.run(
            ["tmux", "new-session", "-d", "-s", tmux_session,
             "-c", PROJECT_ROOT, cmd],
            capture_output=True, text=True, timeout=20,
        )
        return res.returncode == 0, ((res.stdout or "") + (res.stderr or "")).strip()[:200]
    except Exception as exc:
        return False, f"tmux 启动失败：{exc}"


def _shell_quote(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def takeover(market, now, dry_run=False):
    """接管：起一段密采样。返回 (是否启动, 说明)。"""
    argv, tmux_session, log_path = takeover_command(market, now)
    if not argv:
        return False, "没有可用的标的 / 采样脚本，无法接管"
    note = f"{now.strftime('%Y-%m-%d %H:%M:%S')} 看护接管：{' '.join(argv)}"
    if dry_run:
        return True, f"dry-run 未执行：{note}"
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(note + "\n")
    except OSError:
        pass
    ok, detail = _spawn(argv, tmux_session, log_path, now)
    return ok, f"{note}｜{'已启动' if ok else '启动失败'} {detail}"


# —— 节流 ——

def allow_wake(market, now, state):
    """投递节流：同市场 WAKE_COOLDOWN_SECONDS 内最多投一次。返回 (可投, 原因)。"""
    day = market_date_of(market, now)
    cell = (state.get("wakes") or {}).get(market) or {}
    if cell.get("date") != day:
        return True, "本交易日首次"
    last = float(cell.get("last_ts") or 0)
    if now.timestamp() - last < WAKE_COOLDOWN_SECONDS:
        return False, f"距上次投递不足 {WAKE_COOLDOWN_SECONDS // 60} 分钟"
    return True, "冷却已过"


def record_wake(market, now, state):
    day = market_date_of(market, now)
    wakes = state.setdefault("wakes", {})
    cell = wakes.get(market) or {}
    if cell.get("date") != day:
        cell = {"date": day, "count": 0}
    cell["count"] = int(cell.get("count") or 0) + 1
    cell["last_ts"] = now.timestamp()
    cell["last_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    wakes[market] = cell
    save_state(state)


def wake_recent(market, now, state):
    """是否在认领窗口内（刚投递过唤醒请求，等会话自己起来）。"""
    cell = (state.get("wakes") or {}).get(market) or {}
    last = float(cell.get("last_ts") or 0)
    return bool(last) and (now.timestamp() - last) < WAKE_RECLAIM_SECONDS


def allow_takeover(market, now, state):
    """接管节流：两段之间至少隔 MIN_RESTART_INTERVAL_SECONDS（防「启动即失败」时狂重试）。"""
    cell = (state.get("takeovers") or {}).get(market) or {}
    last = float(cell.get("last_ts") or 0)
    if last and now.timestamp() - last < MIN_RESTART_INTERVAL_SECONDS:
        return False, f"距上次接管不足 {MIN_RESTART_INTERVAL_SECONDS} 秒"
    return True, "可接管"


def record_takeover(market, now, state, ok):
    takeovers = state.setdefault("takeovers", {})
    cell = takeovers.get(market) or {"count": 0, "failed": 0}
    cell["count"] = int(cell.get("count") or 0) + 1
    if not ok:
        cell["failed"] = int(cell.get("failed") or 0) + 1
    cell["last_ts"] = now.timestamp()
    cell["last_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    takeovers[market] = cell
    save_state(state)


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
    sessions = market_sessions(market, now)
    argv, _tmux, _log = takeover_command(market, now)
    plan = ("可接管：<" + " ".join(argv) + ">" if argv else "没有可接管标的")
    return (f"{market}：段进程不在、最后一份日志 "
            f"{'无' if age is None else str(int(age)) + ' 秒前'} → 应恢复"
            f"｜可投递信箱的会话 {len(sessions)} 个｜{plan}")


def main():
    ap = argparse.ArgumentParser(description="盯盘中断外部看护（独立于会话、全自动、无外部依赖）")
    ap.add_argument("--status", action="store_true", help="只打印判定与节流状态，不做任何动作")
    ap.add_argument("--dry-run", action="store_true", help="只判定并打印将要执行的动作，不写信箱、不起进程")
    ap.add_argument("--force", action="store_true", help="忽略窗口与节流，按当前事实立即判定并执行")
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
        print(f"  投递记录：{json.dumps(state.get('wakes') or {}, ensure_ascii=False)}")
        print(f"  接管记录：{json.dumps(state.get('takeovers') or {}, ensure_ascii=False)}")
        pend = pending_wake_requests(market, now) if market else []
        print(f"  未认领的唤醒请求：{[p for p, _a in pend]}")
        return 0

    if market is None and not args.force:
        return 0
    market = market or "US"

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
    sessions = market_sessions(market, now)[:2]

    # ① 有会话在盯过这个市场 → 往它的信箱投唤醒请求（给 300 秒认领时间，期间不接管）
    delivered = []
    if sessions:
        if args.force:
            ok_wake = True
        else:
            ok_wake, _why = allow_wake(market, now, state)
        if ok_wake:
            for entry in sessions:
                ok, path = write_wake_request(entry, market, age or 0, now, dry_run=args.dry_run)
                delivered.append((entry[1], ok, path))
            if not args.dry_run and any(d[1] for d in delivered):
                record_wake(market, now, state)
        elif wake_recent(market, now, state):
            pass

    if sessions and wake_recent(market, now, state):
        print(f"[sampling-watchdog] {now.strftime('%Y-%m-%d %H:%M:%S')}｜{market}｜"
              f"断 {int(age or 0)} 秒｜投递唤醒请求：{delivered or '（冷却中，本轮不重复投递）'}"
              f"｜接管 — 等会话认领（{WAKE_RECLAIM_SECONDS} 秒窗口）")
        return 0

    # ② 没有会话、或投递后仍没人认领 → 自己接管
    if args.force:
        took, take_detail = takeover(market, now, dry_run=args.dry_run)
    else:
        ok_take, why_take = allow_takeover(market, now, state)
        if ok_take:
            took, take_detail = takeover(market, now, dry_run=args.dry_run)
            if not args.dry_run:
                record_takeover(market, now, state, took)
        else:
            took, take_detail = False, f"接管节流：{why_take}"

    print(f"[sampling-watchdog] {now.strftime('%Y-%m-%d %H:%M:%S')}｜{market}｜"
          f"断 {int(age or 0)} 秒｜投递唤醒请求：{delivered or '（无会话可投）'}"
          f"｜接管 {'✅' if took else '—'} {take_detail[:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
