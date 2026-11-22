#!/usr/bin/env python3
"""盯盘中断外部看护（2026-11-22 立，T202HKUS；同日按用户要求改为全自动，不外发通知）。

为什么需要它（2026-11-20 实录）：美股会话在 ET 11:03 采样链断掉之后，**再没有发生任何回合**
（没有后台任务完成通知、AI 不会自己醒来）。而 `monitor_guard.py` 的断链兜底挂在
`agent_settled`（回合静止）上——没有回合就没有触发点，那个兜底再准也发不出提醒。
结果是整个后半场无人盯盘、约 5 小时后才被用户发现，停盯收尾迟到约 24 小时。

**定位：全自动、不依赖人工介入（2026-11-22 用户立）**。所以它既不发飞书、也不弹窗喊人，
只做两件自动动作：

  ① **优先叫醒会话**：盘中本场断链时，用 `pi-intercom` 给盯这个市场的会话发一条消息
     （消息进来就会触发一个回合，AI 于是能重启采样段并继续分析）；
  ② **叫不醒就自己接上**：没有可唤醒的会话（会话已死 / 不在线 / intercom 不可用）时，
     看护自己按「本场最后在盯的标的与模式」用 tmux 起下一段密采样——数据链不再断，
     直到 AI 回来接管（AI 一回到场，它自己的段就在跑，看护随即静默）。
     接管痕迹写在 `tmp/sampling_watchdog_takeover_<市场>.log`，AI 回来后读一眼就知道
     「这段时间是看护代跑的，分析要补」。

判定只看三件事实（每次 60 秒巡检）：

  ① **窗口**：直接复用 `monitor_guard.in_hk_session` / `in_us_session`（同一套窗口口径，
     不另写一份，避免两处漂移）；
  ② **本场在盯**：存在**本市场**、**本场日期**（北京 / 美东双日历）的 `monitor_log`——
     只盯过港股、现在美股盘中的场景不会误判；空仓日 / 盘外静默；
  ③ **真断了**：段进程不在（`sampling_process_alive`）+ 本市场最后一份日志已超过
     `GAP_ALERT_SECONDS`（300 秒；正常段间空窗只有几秒）。

节流：叫醒同一市场 10 分钟内最多一次（避免刷屏会话）；自己接管两段之间至少隔
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
    python3 hooks/sampling_watchdog.py --dry-run     # 只判定、只打印将要执行的命令，不发送、不起进程
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
INTERCOM = os.path.expanduser("~/.pi/agent/npm/node_modules/.bin/pi-intercom")

GAP_ALERT_SECONDS = 300           # 本市场最后一份 log 超过这个时长 = 断了（正常段间空窗 <90 秒）
COOLDOWN_SECONDS = 600            # 同一市场的两次「叫醒会话」最小间隔
MIN_RESTART_INTERVAL_SECONDS = 120  # 两次「自己接管起段」的最小间隔
MAX_TARGETS = 4                   # 自己接管时最多盯几只（按最后一份日志的标的顺序取）
SEGMENT_SECONDS = 40              # 与 trade skill 的 40 秒密采样一致（脚本内部也夹到 40）
SEGMENT_INTERVAL = 10
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


# —— 动作一：叫醒会话 ——

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


def live_sessions():
    """在线会话 {session_id: {name, cwd}}（pi-intercom list --json）。

    为什么必须查：pi-intercom 的 `--to <id>` 在目标不在线时会**退回到「唯一的在线会话」**
    （2026-11-22 实测：传一个不存在的 id，消息被投递给另一个会话）。看护的消息内容是
    「盘中断链、立刻起采样段」，投错会话等于对无关的 agent 谎报军情。所以只对**确认在线、
    且 cwd 在本项目（主工作区或本项目 worktree）**的会话发送。
    """
    if not os.path.isfile(INTERCOM):
        return {}
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
    return cwd == root or cwd.startswith(root + os.sep) or cwd.startswith(root + "-")


def intercom_text(market, gap_sec, now):
    label = "美股" if market == "US" else "港股"
    gap_min = max(1, int(gap_sec // 60))
    return (
        f"⚠️ 外部看护（sampling_watchdog）｜{label}盘中密采样已停 {gap_min} 分钟："
        f"本场最后一份 monitor_log 是 {gap_min} 分钟前，且段进程不在。\n"
        f"这是「上一段跑完没起下一段」的形态，而会话已没有回合、`agent_settled` 兜底发不出来，"
        f"所以才由外部直接叫你。\n"
        f"请立刻 background_run 起下一段密采样（标的与关键位按本场最后一份 actions 记录 / "
        f"最后一份 monitor_log 的文件名恢复），并补上这段时间缺的分析；"
        f"若确已到停盯边界，走停盯收尾（stop_summary → keepawake_off → monitor_unregister）。\n"
        f"（若看护已自行代跑过几段，痕迹在 tmp/sampling_watchdog_takeover_{market}.log。）"
    )


def wake_sessions(market, gap_sec, now, dry_run=False):
    """叫醒盯这个市场的会话；返回 [(sid, ok, detail)]。"""
    results = []
    candidates = owner_sessions(market, now)[:2]
    if not candidates:
        return []
    if dry_run:
        return [(sid, True, "dry-run 未发送") for _u, sid, _s in candidates]
    if not os.path.isfile(INTERCOM):
        return [(sid, False, "pi-intercom 不存在") for _u, sid, _s in candidates]
    live = live_sessions()
    if not live:
        return [(sid, False, "intercom 无在线会话") for _u, sid, _s in candidates]
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
    """叫醒节流：同市场 COOLDOWN_SECONDS 内最多一次。返回 (可发, 原因)。"""
    day = market_date_of(market, now)
    cell = (state.get("wakes") or {}).get(market) or {}
    if cell.get("date") != day:
        return True, "本交易日首次"
    last = float(cell.get("last_ts") or 0)
    if now.timestamp() - last < COOLDOWN_SECONDS:
        return False, f"距上次叫醒不足 {COOLDOWN_SECONDS // 60} 分钟"
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
    argv, tmux_session, _log = takeover_command(market, now)
    plan = ("可接管：<" + " ".join(argv) + ">" if argv else "没有可接管标的")
    return (f"{market}：段进程不在、最后一份日志 "
            f"{'无' if age is None else str(int(age)) + ' 秒前'} → 应恢复｜{plan}")


def main():
    ap = argparse.ArgumentParser(description="盯盘中断外部看护（独立于 AI 会话，全自动）")
    ap.add_argument("--status", action="store_true", help="只打印判定与节流状态，不做任何动作")
    ap.add_argument("--dry-run", action="store_true", help="只判定并打印将要执行的命令，不发送、不起进程")
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
        print(f"  叫醒记录：{json.dumps(state.get('wakes') or {}, ensure_ascii=False)}")
        print(f"  接管记录：{json.dumps(state.get('takeovers') or {}, ensure_ascii=False)}")
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
    woke = []
    if args.force:
        woke = wake_sessions(market, age or 0, now, dry_run=args.dry_run)
    else:
        ok_wake, why = allow_wake(market, now, state)
        if ok_wake:
            woke = wake_sessions(market, age or 0, now, dry_run=args.dry_run)
        else:
            why = f"叫醒节流：{why}"
        if not args.dry_run and woke:
            record_wake(market, now, state)

    # 叫不醒（没有候选 / 全都失败）→ 自己把链接回去
    woke_ok = any(r[1] for r in woke)
    took, take_detail = (False, "已有会话被叫醒，交给它重启")
    if not woke_ok:
        if args.force:
            took, take_detail = takeover(market, now, dry_run=args.dry_run)
        else:
            ok_take, why_take = allow_takeover(market, now, state)
            if ok_take:
                took, take_detail = takeover(market, now, dry_run=args.dry_run)
                if not args.dry_run:
                    record_takeover(market, now, state, took)
            else:
                take_detail = f"接管节流：{why_take}"

    print(f"[sampling-watchdog] {now.strftime('%Y-%m-%d %H:%M:%S')}｜{market}｜"
          f"断 {int(age or 0)} 秒｜叫醒 {woke if woke else '（无可唤醒会话）'}｜"
          f"接管 {'✅' if took else '—'} {take_detail[:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
