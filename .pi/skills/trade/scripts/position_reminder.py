#!/usr/bin/env python3
"""持仓定时飞书提醒（2026-11-22 立，首个用例 = TODO T203US 的 TQQQ 空头）。

用途：某个具体持仓必须在某个时间点之前处置（例如「美股正式开盘前把某只空头平掉」）时，
按计划表在指定的美东时刻自动发一条飞书提醒。整条链路与盯盘会话、与 AI 的记忆完全无关——
时间到了就会发，不依赖任何会话活着。

为什么是这套结构（沿用 power_reminder.py 已验证的做法）：
  - 定时器部署在常开的 macmini。本机 MacBook 合盖且没接电源时会休眠，休眠期间 launchd
    不触发，而「该处置的持仓还没处置」恰恰是最需要收到提醒的场景。
  - plist 每 60 秒巡检一次、而不是用 StartCalendarInterval 定点排程。后者按**系统本地
    时区**排程，美东的某个时刻换算成北京时间会随冬夏令时变化，写死任何一个都会错位；
    脚本内部用 zoneinfo 取真实美东时间，自动处理夏令时与冬令时。
  - 每个事件发完就写一条标记（并用文件锁防同机并发），重复巡检不会重复发同一条。
  - 发送走同目录 notify_feishu.py（带 --no-mark --no-session-id），不另写一套发送逻辑，
    所以内容过滤、接收人校正、NO_PROXY 直连等口径与其它飞书通知完全一致。

计划表（本机文件、不入库）：默认 state/position_reminders.json，可用环境变量
POSITION_REMINDER_CONFIG 指定别的位置。格式见同目录上一级 position_reminders.example.json：

    {"events": [
       {"id": "t203us-1",
        "at_et": "2026-11-22 21:00",
        "window_min": 15,
        "text": "🔔 **持仓提醒**：……"}
    ]}

字段说明：
  id          事件标识，决定标记文件名（state/position_reminder_sent_<id>.json）；必填
  at_et       美东本地时刻，格式 `YYYY-MM-DD HH:MM`（按美东墙钟解释）；必填
  window_min  补发窗口（分钟，默认 15）。定时任务因休眠 / 重启错过准点，窗口内仍会发；
              超出窗口即判定「错过」，只记日志、不再补发——过时的提醒会误导人，比不发更糟
  text        消息正文（markdown）；必填。走 notify_feishu.py 的内容过滤，只发业务内容
  market      归属市场（us / hk，默认 us），只影响 notify_feishu.py 的口径参数

用法：
    python3 position_reminder.py                    # 定时巡检（launchd 每 60 秒调用）
    python3 position_reminder.py --status           # 打印计划表与每个事件的状态（人工核对）
    python3 position_reminder.py --dry-run          # 只判断、只做飞书内容过滤校验，不发不写标记
    python3 position_reminder.py --send-now <id>    # 立即实发指定事件（链路自检用，会写标记）

退出码：0 正常（含「没有待发事件」）/ 1 有事件发送失败 / 2 计划表缺失或格式错误。
"""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# scripts/ → .pi/skills/trade → .pi/skills → .pi → 项目根
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "..", ".."))
NOTIFY = os.path.join(SCRIPT_DIR, "notify_feishu.py")
STATE_DIR = os.path.join(PROJECT_ROOT, "state")
PLAN_PATH = os.environ.get("POSITION_REMINDER_CONFIG") or os.path.join(
    STATE_DIR, "position_reminders.json")

ET = ZoneInfo("America/New_York")
BJT = ZoneInfo("Asia/Shanghai")
DEFAULT_WINDOW_MIN = 15


def now_et(explicit=None):
    """取当前美东时间；`explicit` 给 'YYYY-MM-DD HH:MM:SS' 时按美东本地时间解释（测试用）。"""
    if explicit:
        return datetime.strptime(explicit, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
    return datetime.now(ET)


def dual_clock(moment):
    """'美东 2026-11-22 21:00 = 北京 2026-11-23 10:00'——ET 在前、北京在括号里（项目 2026-11-05 立）。"""
    return (f"美东 {moment.strftime('%Y-%m-%d %H:%M')}"
            f" = 北京 {moment.astimezone(BJT).strftime('%Y-%m-%d %H:%M')}")


def marker_path(event_id):
    return os.path.join(STATE_DIR, f"position_reminder_sent_{event_id}.json")


def lock_path():
    # 固定名字、不按事件分，避免事件多了留一堆锁文件。
    return os.path.join(STATE_DIR, "position_reminder.lock")


def load_plan(path=None):
    """读计划表并校验；返回事件列表（at 已解析成带美东时区的 datetime）。"""
    path = path or PLAN_PATH
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    items = raw.get("events") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise ValueError("计划表顶层应为 {\"events\": [...]}")

    events, seen = [], set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个事件不是对象")
        event_id = str(item.get("id") or "").strip()
        text = str(item.get("text") or "").strip()
        at_raw = str(item.get("at_et") or "").strip()
        if not event_id or not text or not at_raw:
            raise ValueError(f"第 {index} 个事件缺 id / at_et / text 之一")
        if event_id in seen:
            raise ValueError(f"事件 id 重复：{event_id}")
        seen.add(event_id)
        try:
            at = datetime.strptime(at_raw, "%Y-%m-%d %H:%M").replace(tzinfo=ET)
        except ValueError:
            raise ValueError(f"事件 {event_id} 的 at_et 格式应为 'YYYY-MM-DD HH:MM'，实际 {at_raw!r}")
        try:
            window = int(item.get("window_min") or DEFAULT_WINDOW_MIN)
        except (TypeError, ValueError):
            raise ValueError(f"事件 {event_id} 的 window_min 不是整数")
        if window <= 0:
            raise ValueError(f"事件 {event_id} 的 window_min 必须为正数")
        events.append({
            "id": event_id,
            "at": at,
            "window": window,
            "text": text,
            "market": str(item.get("market") or "us").strip() or "us",
        })
    events.sort(key=lambda e: e["at"])
    return events


def read_marker(event_id):
    try:
        with open(marker_path(event_id), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_marker(event, sent_at_et, window, output):
    os.makedirs(STATE_DIR, exist_ok=True)
    match = re.search(r"message_id=([^；)]+)", output or "")
    payload = {
        "id": event["id"],
        "at_et": event["at"].strftime("%Y-%m-%d %H:%M"),
        "window": window,
        "sent_at_et": sent_at_et,
        "message_id": match.group(1).strip() if match else "",
    }
    path = marker_path(event["id"])
    tmp = path + ".tmp"
    # 先写临时文件再改名，避免两次触发同时写坏同一个标记。
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def event_state(event, now):
    """返回 sent / due / missed / pending 四态之一。"""
    if read_marker(event["id"]):
        return "sent"
    if now < event["at"]:
        return "pending"
    if now < event["at"] + timedelta(minutes=event["window"]):
        return "due"
    return "missed"


def feishu_config_override():
    """按本机 lark-cli 绑定的飞书应用校正接收人；不需要校正时返回 None。

    直接复用 power_reminder.py 里已经验证过的那一份实现（同一套 open_id 与应用绑定口径）——
    两处各写一套必然漂移，而漂移的后果是「提醒在那台机器上根本发不出去」。
    导入失败时返回 None：不阻断发送，回退到项目里同步过来的 feishu.local.json。
    """
    try:
        sys.path.insert(0, SCRIPT_DIR)
        from power_reminder import feishu_config_override as _impl
        return _impl()
    except Exception:
        return None


def send(event, dry_run=False):
    """调 notify_feishu.py 发一条；返回 (是否成功, 输出文本)。"""
    if not os.path.isfile(NOTIFY):
        return False, f"⛔ 找不到飞书发送脚本：{NOTIFY}"
    cmd = [sys.executable, NOTIFY, "--text", event["text"],
           "--market", event["market"],
           "--date", event["at"].strftime("%Y-%m-%d"),
           "--no-mark", "--no-session-id"]
    if dry_run:
        cmd.append("--dry-run")
    env = dict(os.environ)
    override = feishu_config_override()
    if override:
        env["FEISHU_CONFIG"] = override
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=env)
    except Exception as exc:
        return False, f"⛔ 调用飞书发送脚本异常：{exc}"
    output = (res.stdout or "") + (res.stderr or "")
    if res.returncode != 0:
        return False, f"⛔ 飞书发送脚本退出码 {res.returncode}：{output.strip()[:300]}"
    # notify_feishu.py 在「飞书已在本机停用」时也返回 0，但消息并没有真的发出去。
    # 这种情形不能写「已发」标记（否则这条提醒就静默漏掉了），也不重试（重试同样发不出去）。
    if not dry_run and "飞书已发送" not in output:
        return False, f"⚠️ 飞书脚本返回成功但未见发送确认（可能已在本机停用）：{output.strip()[:200]}"
    return True, output


def status_lines(events, now, path=None):
    """给 --status 用：逐条陈述每个事件的状态，不发任何消息。"""
    if not events:
        return [f"📋 计划表：{path or PLAN_PATH}（没有任何事件）"]
    lines = [f"📋 计划表：{path or PLAN_PATH}（共 {len(events)} 个事件，当前 {dual_clock(now)}）"]
    for event in events:
        state = event_state(event, now)
        if state == "sent":
            mark = read_marker(event["id"]) or {}
            lines.append(f"  ✅ {event['id']}｜{dual_clock(event['at'])}｜已发"
                         f"（{mark.get('sent_at_et', '时间未记')}，{mark.get('window', '窗口未记')}）")
        elif state == "due":
            lines.append(f"  🟡 {event['id']}｜{dual_clock(event['at'])}｜窗口内待发（下次巡检即发）")
        elif state == "missed":
            lines.append(f"  ❌ {event['id']}｜{dual_clock(event['at'])}｜已错过补发窗口"
                         f"（{event['window']} 分钟），不再补发")
        else:
            delta = event["at"] - now
            hours, rest = divmod(int(delta.total_seconds()), 3600)
            lines.append(f"  ⏳ {event['id']}｜{dual_clock(event['at'])}｜待发"
                         f"（还有 {hours} 小时 {rest // 60} 分）")
    return lines


def cmd_check(events, now, dry_run=False):
    """定时巡检：把窗口内的事件发掉。返回退出码。"""
    if not os.path.isdir(STATE_DIR):
        os.makedirs(STATE_DIR, exist_ok=True)
    try:
        lock_file = open(lock_path(), "a+")
    except OSError as exc:
        print(f"⛔ 打不开去重锁文件：{exc}", file=sys.stderr)
        return 1

    with lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0  # 另一个实例正在处理，本次直接退出

        # 拿到锁之后重新读一次状态：可能另一个实例刚刚发完并写好了标记。
        due = [e for e in events if event_state(e, now) == "due"]
        if not due:
            return 0
        failed = 0
        for event in due:
            sent_at_et = now.strftime("%Y-%m-%d %H:%M:%S ET")
            window = "准点" if now < event["at"] + timedelta(minutes=2) else "窗口内补发"
            ok, output = send(event, dry_run=dry_run)
            print(f"[position-reminder] {event['id']}（{dual_clock(event['at'])}，{window}）："
                  f"{output.strip()[:400]}")
            if not ok:
                failed += 1
                continue
            if not dry_run:
                try:
                    write_marker(event, sent_at_et, window, output)
                except Exception as exc:
                    # 消息已经发出去了，标记写失败不能再发一遍，否则用户会收到两条。
                    print(f"⚠️ {event['id']} 已发出，但标记写入失败（{exc}）", file=sys.stderr)
        return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description="持仓定时飞书提醒（按美东时刻的计划表）")
    parser.add_argument("--status", action="store_true", help="只打印计划表与各事件状态（不发消息）")
    parser.add_argument("--dry-run", action="store_true", help="只判断并做飞书内容过滤校验，不发、不写标记")
    parser.add_argument("--send-now", metavar="ID", help="立即实发指定事件（链路自检用，会写标记）")
    parser.add_argument("--config", help="指定计划表文件（默认 state/position_reminders.json）")
    parser.add_argument("--now", help="指定美东本地时间 'YYYY-MM-DD HH:MM:SS'（测试用）")
    args = parser.parse_args()

    try:
        now = now_et(args.now)
    except ValueError:
        print("⛔ --now 格式应为 'YYYY-MM-DD HH:MM:SS'（美东本地时间）", file=sys.stderr)
        return 2

    try:
        events = load_plan(args.config)
    except FileNotFoundError:
        print(f"⛔ 计划表不存在：{args.config or PLAN_PATH}——没有可发的事件（这不影响其它功能）",
              file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"⛔ 计划表读取失败：{exc}", file=sys.stderr)
        return 2

    if args.status:
        for line in status_lines(events, now, args.config):
            print(line)
        return 0

    if args.send_now:
        event = next((e for e in events if e["id"] == args.send_now), None)
        if not event:
            print(f"⛔ 计划表里没有 id 为 {args.send_now} 的事件", file=sys.stderr)
            return 2
        ok, output = send(event, dry_run=args.dry_run)
        print(f"[position-reminder] {event['id']} 手工发送：{output.strip()[:400]}")
        if ok and not args.dry_run:
            write_marker(event, now.strftime("%Y-%m-%d %H:%M:%S ET"), "手工触发", output)
        return 0 if ok else 1

    return cmd_check(events, now, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
