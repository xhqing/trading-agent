#!/usr/bin/env python3
"""盯盘开盘前检查：当前时间 / 港股美股时段 / 富途 OpenD。
用法: python3 preflight.py
一行汇总就绪状态,避免开盘才发现数据源掉线或时间误判。"""
import datetime, socket

now = datetime.datetime.now()
hhmm = now.hour * 60 + now.minute
wd = now.weekday()  # 0=Mon..6=Sun
print(f"⏰ {now.strftime('%Y-%m-%d %H:%M:%S %A')}")

# 港股时段 (UTC+8, 与北京同时区)
if wd >= 5:
    hk = "周末休市"
elif 570 <= hhmm < 720:
    hk = "港股早市 09:30-12:00"
elif 780 <= hhmm < 960:
    hk = "港股午市 13:00-16:00"
else:
    hk = "盘外"
# 美股时段（2026-08-18 修订：美股可交易窗口扩为「盘前 + 盘中」= 美东 04:00-16:00——
#   盘前 04:00-09:30 与盘中 09:30-16:00 都可开仓 / 平仓 / 移损 / 发信号，16:00 收盘边界不变；
#   2026-07-19 旧规「仅盘中 09:30-16:00」废止。换算北京时间随美东夏/冬令时切换：
#     夏令时 EDT(UTC-4)：美东 04:00-16:00 = 北京 16:00-次日 04:00（跨午夜）
#     冬令时 EST(UTC-5)：美东 04:00-16:00 = 北京 17:00-次日 05:00（跨午夜）
#   直接用 zoneinfo 把当前时间转到美东时区判「周末 / 时段 / DST」，不再用北京本地反推——
#   冬令时盘中跨午夜段（北京周六凌晨 00:00-05:00 实为美东周五盘中）不会被本地周末判断误伤。
#   zoneinfo 不可用时回退北京夏令时估并标注。）
def _us_status():
    try:
        from zoneinfo import ZoneInfo
        us_now = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        # zoneinfo 不可用：回退北京本地估夏令时（仅兜底，冬令时段可能不准）
        if wd >= 5:
            return "周末休市"
        # 夏令时估：美东 04:00-16:00 = 北京 16:00-次日 04:00（跨午夜）
        if 960 <= hhmm < 1440 or hhmm < 240:
            return "美股可交易(夏令时估·zoneinfo不可用)·盘前+盘中均可(美东04:00-16:00)"
        return "美股盘外(夏令时估·zoneinfo不可用)·不发信号"
    us_wd = us_now.weekday()
    us_hhmm = us_now.hour * 60 + us_now.minute
    tz_tag = "EDT夏令时" if bool(us_now.dst()) else "EST冬令时"
    if us_wd >= 5:
        return "周末休市"
    if 240 <= us_hhmm < 960:  # 美东 04:00-16:00（盘前 04:00-09:30 + 盘中 09:30-16:00）
        phase = "盘前" if us_hhmm < 570 else "盘中"
        return f"美股可交易({phase}·{tz_tag})·可发信号(美东04:00-16:00，盯到用户喊停或收盘)"
    return f"美股盘外({tz_tag})·不发信号"

us = _us_status()
print(f"📈 港股:{hk} | 美股:{us}")

# 富途 OpenD 端口（盯盘行情主力源，须登录成功才监听 11111）
def port_open(p):
    s = socket.socket(); s.settimeout(1)
    try:
        s.connect(('127.0.0.1', p)); s.close(); return True
    except Exception:
        return False
print(f"📊 富途OpenD:11111 {'✅' if port_open(11111) else '❌(未登录/未启动)'}")

# 2026-08-01 双模式重构：风险比例 + 权益（按模式取：auto 走账户 API、signal 走权益账本）
import json as _json, os as _os, sys as _sys
try:
    _sys.path.insert(0, _os.path.dirname(__file__))
    from trade_utils_tiger import load_equity as _le, parse_mode as _pm
    _mode = _pm()
    # 账户选择（2026-08-20 立，实盘盯盘配套）：--account live 切实盘账户取风险基数。
    # 事故（2026-08-20）：实盘盯盘时 preflight 固定走默认模拟账户算 B（模拟盘的资金量级
    # 远大于实盘），B 与实盘真实风险基数偏差 68 倍。修复 = 取权益与下单同账户（load_equity 透传）。
    # 2026-09-12 解耦：auto + 模拟盘（默认 / --account paper）equity 读 equity-log 纸面权益
    # （与 signal 同源，见 trade_utils_tiger.paper_sizing_reference）——不再依赖实盘当日快照。
    _acct = None
    for _i, _a in enumerate(_sys.argv[1:]):
        if _a == '--account' and _i + 1 < len(_sys.argv[1:]):
            _acct = _sys.argv[1:][_i + 1].lower()
            _acct = _acct if _acct in ('live', 'paper') else None
    _cfg_path = _os.path.join(_os.path.dirname(__file__), '..', 'config.json')
    with open(_cfg_path) as _f:
        _risk = _json.load(_f).get('risk', {})
    _frac = _risk.get('risk_fraction'); _fmax = _risk.get('f_max', _frac)
    _lev = _risk.get('max_leverage', 10)
    _eq_now, _cur, _eq_src = _le(_mode, account=_acct)
    # 2026-09-12 解耦（模拟盘与实盘资产解耦）：auto 模拟盘算仓位 equity 从 equity-log 纸面
    # 权益读（与 signal 同源），购买力 = 纸面权益 × 模拟盘总融资杠杆 × 标的保证金率——全程
    # 不碰实盘、无快照依赖，实盘资产为 0 / 资金取出不影响模拟盘交易。旧「实盘参考快照联动」
    # （2026-09-03 立：实盘会话刷快照、模拟盘会话缺快照即拒单）废止；--refresh-live-reference
    # CLI 保留作诊断工具。
    if _frac is not None and _eq_now is not None:
        _M = _frac * _eq_now
        print(f"💰 模式 {_mode} | 风险比例 {_frac*100:.1f}% × 当前 equity {_eq_now:,.2f} {_cur} = 单笔预算 B {_M:,.2f}（f_max 硬上限 {_fmax*100:.1f}%，max_loss 不得突破）")
        print(f"   equity 来源：{_eq_src}")
        print(f"⚖️  开仓市值上限 = equity × {_lev} 倍杠杆 = {_eq_now * _lev:,.2f} {_cur}（权益 {_eq_now:,.0f} → 最高开仓 {_eq_now * _lev:,.0f} 市值；选仓位时 max_loss 与市值两约束同时满足）")
    elif _mode == "auto" and _acct != "live" and _eq_now is None:
        # auto 模拟盘且实盘快照取不到：B 行已在上面 🚨 警示里说明原因，这里不再重复打。
        pass
    else:
        print(f"💰 ⚠️ config 缺 risk_fraction，盘中算仓位前务必手动确认")
except Exception as _e:
    print(f"💰 ⚠️ 读取 config/equity 失败({_e})，盘中算仓位前务必手动确认 risk.risk_fraction")

# positions 检查已移除（2026-07-15 信号模式：假设执行、不查 positions，见 SKILL「信号模式总则」第 1 条）

# 防系统睡眠（2026-07-25 立 → 2026-07-27 修订 → 2026-11-03 修正能力边界）
# 根因：盯盘期间系统睡眠会暂停所有进程——富途 OpenD 的 get_market_snapshot 无 timeout、
# 卡到 TCP 超时 ~15 分钟才返回、整段采样空窗。
# ⚠️ **能力边界（2026-11-03 日志实测修正）**：caffeinate -s 只能防「空闲睡眠 / 维护睡眠」，
# **防不住合盖睡眠（Clamshell Sleep）**——电池供电下合盖是 macOS 硬件强制睡眠，
# caffeinate 断言仍在依旧会睡（旧注释误写「防合盖 Clamshell」，已纠正）。
# 2026-11-03 00:02 实录：合盖 + 电池（31%）→ `Entering Sleep state due to 'Clamshell Sleep'`，
# 富途请求被挂起 33 分钟、会话停摆近 4 小时，停盯收尾靠一次 45 秒的 DarkWake 窗口才侥幸完成。
# 故由独立定时任务在每天美东 09:30 发一条「插电源」飞书提醒（power_reminder.py），
# preflight 只打印当日发送状态、不再自己发（2026-11-12：多会话并行会重复通知）。
def _ensure_awake():
    import subprocess as _sp, time as _t
    if _sp.run(["pgrep", "-f", "caffeinate -s"], stdout=_sp.DEVNULL).returncode == 0:
        print("☕ caffeinate -s 已在跑（盯盘防系统睡眠；停盯时自动解除）")
        return
    try:
        _sp.Popen(["caffeinate", "-s"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        _t.sleep(0.5)
        if _sp.run(["pgrep", "-f", "caffeinate -s"], stdout=_sp.DEVNULL).returncode == 0:
            print("☕ caffeinate -s 已启动（盯盘防系统睡眠；停盯时自动解除）")
        else:
            print("⚠️ caffeinate 启动失败（盯盘期间注意别让系统睡眠）")
    except Exception as _e:
        print(f"⚠️ 启用防睡眠失败（{_e}）（盯盘期间注意别让系统睡眠）")


def _power_reminder_status():
    """在场打印「插电源提醒」当日发送状态（2026-11-12 起不再由 preflight 发送）。

    为什么改：原实现每次 preflight 都发一条飞书提醒，多会话并行盯盘时同一条消息会
    重复发。现在由 macmini 上的 launchd 定时任务在美东 09:30 统一发一条，与盯盘
    会话数量无关。preflight 保留这一行状态打印，是为了让「今天到底发没发」在盯盘
    启动这个决策时刻仍然可见（本项目「文档规定必须尽可能配工具强制」的第②层：
    决策时刻工具在场打印），不依赖 AI 记忆，也不再触发任何发送动作。
    """
    import os as _os, subprocess as _sp, sys as _sys
    script = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "power_reminder.py")
    if not _os.path.isfile(script):
        print("⚠️ 电源提醒定时脚本缺失——请自行确认电源线两头都已插牢")
        return
    try:
        r = _sp.run([_sys.executable, script, "--status"],
                    capture_output=True, text=True, timeout=15)
        out = (r.stdout or "").strip()
        print(out or "🔌 电源提醒：由定时任务于美东 09:30 统一发送（不由盯盘会话发送）")
    except Exception as _e:
        print(f"🔌 电源提醒：状态检查失败（{_e}）——请自行确认电源线两头都已插牢")


_ensure_awake()


def _worktree_check():
    """盯盘必须在 worktree 里跑（2026-11-03 用户立，工具在场提醒）。

    用户原话：「本项目盯盘任务必然会在工作区写入文件，为了减少盘中干扰，在用户发出盯盘指令
    的时候直接先准备好 worktree 并切换到 worktree 开始盯盘。」

    为什么：盯盘持续写 `actions/` `signals/` 等被 git 跟踪的文件，而主工作区（main）自
    2026-11-02 起禁止 `git add` / `git commit`（`git-main-guard.ts` 硬拦）——在主工作区跑
    会把改动积在 main 上、最后提交不了，只能手工搬去 worktree。

    判定：`--git-dir` 与 `--git-common-dir` 不同 → 在 worktree（前者在
    `<common>/worktrees/<name>`）；相同 → 主工作区。
    """
    import os as _os, subprocess as _sp
    try:
        r1 = _sp.run(["git", "rev-parse", "--git-dir"], capture_output=True, text=True, timeout=5)
        r2 = _sp.run(["git", "rev-parse", "--git-common-dir"], capture_output=True, text=True, timeout=5)
        r3 = _sp.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
        if r1.returncode or r2.returncode or r3.returncode:
            return  # 非 git 仓库：不管
        gd = _os.path.realpath(r1.stdout.strip())
        cd = _os.path.realpath(r2.stdout.strip())
        top = _os.path.realpath(r3.stdout.strip())
        if gd == cd:
            print("⚠️ 盯盘未在 worktree 里：当前是主工作区（main 分支）——盯盘会写 actions/ signals/，"
                  "而 main 已禁止 add / commit，改动最后提交不了。")
            print("   正确动作（2026-11-03 用户立）：先跑 "
                  "`python3 .pi/skills/trade/scripts/worktree_start.py --market us|hk`，"
                  "再用它输出的 WORKTREE 路径重跑本脚本及后续所有盯盘命令。")
        else:
            print(f"🧵 盯盘 worktree：{top}")
    except Exception:
        pass  # 检查失败不阻断 preflight


_worktree_check()
_power_reminder_status()

# D12（2026-08-04）：盯盘启动密采样入口提醒——防 AI 用 cron / 直接 snapshot 绕过 monitor_segment 降频
# （2026-08-04 教训，多层防护见 monitoring.md「不因市况降频」节 + hooks/monitor_guard.py）。
print("🔒 密采样提醒：盯盘密采样唯一入口是 monitor_segment.py 40 秒循环，禁用 cron / 直接 snapshot 替代。")

# ── 刷新窗口 ①：盯盘启动时刷新本地 IP 池（2026-10-30 用户立）─────────────────
# 用户口径：**只有两个窗口允许拉取 JMS 订阅**——① 收到盯盘指令时（就是这里）；
# ② 收盘后写总结前（stop_summary.py）。盘中一律禁止拉订阅（jms_pool.fetch_subscription
# 会直接返回 blocked_intraday），只用本地 IP 池、失败就在池内换端口。
# 这里必须传 allow_intraday=True：盯盘启动往往正好在交易时段内。
try:
    import jms_pool as _J
    _r = _J.refresh(force=True, allow_intraday=True)
    if _r.get('ok'):
        import ip_pool as _P
        _m = _P.start_multi(force=True)
        print(f"🌐 IP 池已刷新（{len(_r.get('nodes') or {})} 个节点 / "
              f"{len(_r.get('ips') or [])} 个 IP，池内 {len(_m or [])} 个端口）"
              + ("　⚠️ 用的是缓存兜底（订阅源本次没拉到）" if _r.get('stale') else ""))
    else:
        print(f"⚠️ IP 池刷新失败（{_r.get('reason')}）——将使用本地缓存；"
              f"盘中不再重试（2026-10-30 口径）")
except Exception as _e:
    print(f"⚠️ IP 池刷新技术异常（{_e}）——将使用本地缓存")

# 交易互斥状态（2026-08-17 立，多会话并行盯盘·方案 A）：启动时一眼看到「谁在场」——
# intent 日志有无 pending（有 = 后到开仓会被拒，先处理）、常驻白名单、在场注册会话数。
try:
    from trade_mutex import pending_intents, resident_positions as _rp
    _pend = pending_intents()
    if _pend:
        print(f"⚠️ 互斥状态：intent 有 {len(_pend)} 条 pending（后到开仓会被拒）——先查当日订单确认无在途单，"
              f"再 python3 scripts/trade_mutex.py --clear-intent <行号> 清掉")
    else:
        print("🔒 互斥状态：intent 链干净（开仓闸门放行）；单持仓由 trade_mutex 强制（多会话先到先得）")
    _rp_list = _rp()
    if _rp_list:
        print(f"📌 常驻持仓白名单（不进开仓链）：{_rp_list}")
except Exception as _e:
    print(f"🔒 互斥状态：读取失败（{_e}）——trade_mutex.py status 手动确认")
# 实盘熔断状态在场打印（2026-09-12 立实盘熔断机制）：熔断中则实盘开仓一律被拒，
# 盯盘启动时让状态在场（散文规定会衰减，工具在场打印全守住——2026-08-18 实证）。
try:
    import circuit_breaker as _CB
    _cb = _CB.read_state()
    if _cb.get("status") == "tripped":
        print(f"🛑 实盘熔断中（{_cb.get('tripped_at', '?')} 触发：{(_cb.get('reason') or '')[:80]}）"
              f"——实盘开仓一律拒单（blocked_by: circuit_breaker）；平仓/移损/移盈不受限；"
              f"模拟盘/信号照常（修整验证场）。解除：复盘重启流程 reset --confirmed-by-user")
    else:
        print("🟢 实盘熔断：未触发（实盘开仓正常，仍需 live 授权解锁 + 一级降频闸等既有闸）")
except Exception as _e:
    print(f"⚠️ 实盘熔断状态读取失败（{_e}）——python3 scripts/circuit_breaker.py status 手动确认")
try:
    _reg = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..", "..", "..", "tmp", "monitor_sessions.txt"))
    if _os.path.isfile(_reg):
        _n = sum(1 for _l in open(_reg) if _l.strip())
        print(f"👀 在场盯盘会话数：{_n}（含本会话；多会话并行时 caffeinate 引用计数、最后停盯才解除防睡眠）")
except Exception:
    pass

# 美股收尾截止检查（2026-09-15 用户立，工具强制·决策时刻在场打印层）：美股收尾硬截止 =
# 美东 24:00（12 midnight ET；夏令时北京次日 12:00 / 冬令时次日 13:00）——美股交易日全部收尾
# 工作（停盯总结 / 漏写补记 / 注销清理）最迟在此前完成、不漏进下一个美东日历日。美股交易日跨
# 北京午夜、会话易中断（模型切换 / 断链），停盯当场没收上尾的，恢复会话 / 次日首个会话第一件事
# 补办——preflight 启动时检测并提醒。启发式口径（已知盲区、宁可少报不误报）：已完结的美股
# 交易日（今日 ET 之前，或今日 ET 且已过 16:00 收盘）的 -ET- 记录文件含动作记录（⏰）但无
# 「停盯总结」字样 → 提醒 / 过线 🚨 催补；多会话日只要任一会话写过总结即不报（单会话粒度
# 无凭据、不可检测）；零记录日不产生文件、天然不报。港股 12:00 / 16:00 边界当场闭环、不查。
def _has_real_action(txt):
    """文件里是否有**真实**动作记录（带 ⏰ 的行、但不属于影子仓条目）。

    T190（2026-11-11 立）：原判据只看「文件里有没有 ⏰」，而影子仓的开仓 / 平仓 / 移损
    条目天然带 `> ⏰ 影子开仓时间：…` / `影子平仓时间` / `影子移损时间`，且写在
    `signals/*-ET-signals.md`（该文件本来就不写停盯总结）——于是只要当天落过影子仓，
    之后连续 7 个自然日里 preflight 每天都报「🚨 美股收尾已过截止线」。2026-11-11 实测
    11-06 / 11-09 / 11-10 三天全部误报，真报警被假警报淹没。

    判据改成「真实动作 = 含 ⏰ 且不含「影子」的行」：真实条目写的是 `> ⏰ 动作时间：…`，
    影子条目一律带「影子」二字（开仓 / 平仓 / 移损三种）。
    """
    for _line in txt.splitlines():
        if "⏰" in _line and "影子" not in _line:
            return True
    return False


def _stop_done_recorded(root, et_date):
    """该 ET 交易日是否已留下停盯收尾标记（`state/stop_done_us_<date>_<stag>.txt`）。

    T190 第二条判据：收尾标记由 `stop_summary.py` / `mark_stop_done.py` 写、跨会话可见，
    是比「文件里有没有某段文字」更直接的「已收尾」证据。标记在 = 不必再催（对应
    `monitor_guard._stop_done_reminder()` 的同一份证据源，两处判定口径一致）。
    """
    try:
        import glob as _g
        return bool(_g.glob(_os.path.join(root, "state", f"stop_done_us_{et_date.isoformat()}*")))
    except Exception:
        return False


def _us_wrapup_check():
    try:
        from zoneinfo import ZoneInfo
        _us_now = datetime.datetime.now().astimezone(ZoneInfo("America/New_York"))
    except Exception as _e:
        print(f"⏹️ 美股收尾截止检查跳过（时区库不可用：{_e}）——手动核对上一美股交易日停盯总结是否已写")
        return
    _root = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                            "..", "..", "..", ".."))
    _days = []
    # 回看最近 7 个自然日内的 ET 交易日（截止线已全部过线 → 🚨）
    for _back in range(1, 8):
        _d = _us_now.date() - datetime.timedelta(days=_back)
        if _d.weekday() < 5:
            _days.append((_d, True))
    # 今日 ET：已过 16:00 收盘则当日也已完结（截止线今晚美东 24:00 → 未过线提醒）
    if _us_now.weekday() < 5 and _us_now.hour >= 16:
        _days.append((_us_now.date(), False))
    for _d, _passed in _days:
        # 收尾标记在 = 该交易日已收尾（无论 signals / actions 里写了什么），不再逐文件报警。
        if _stop_done_recorded(_root, _d):
            continue
        for _dir, _name in (("signals", f"{_d.isoformat()}-ET-signals.md"),
                            ("actions", f"{_d.isoformat()}-ET-actions.md")):
            _fp = _os.path.join(_root, _dir, _name)
            try:
                _txt = open(_fp, encoding="utf-8").read()
            except Exception:
                continue
            if _has_real_action(_txt) and "停盯总结" not in _txt:
                if _passed:
                    print(f"🚨 美股收尾已过截止线：{_d.isoformat()} ET 的 {_dir}/{_name} 有动作记录但无"
                          f"停盯总结——按硬规（美东 24:00 收尾截止）立即补写总结，不等")
                else:
                    print(f"⏹️ 美股收尾提醒：{_d.isoformat()} ET 的 {_dir}/{_name} 尚无停盯总结——当日收尾"
                          f"最迟美东 24:00（夏令时北京次日 12:00 / 冬令时次日 13:00）前完成")

_us_wrapup_check()

# 飞书汇报漏发检查（2026-10-13 用户立）：每次停盯都要把停盯总结（业务版）推送到飞书，
# 本检查看「最近一个已完结的美股交易日」已在 signals/actions 写入停盯总结、但
# notify_feishu.py 的发送标记（state/feishu_sent_us_<交易日>.txt）不存在 → 提醒补发。
# 2026-11-04 修：标记自状态/临时目录分离（2026-11-03）后写在 state/，本检查原先仍读 tmp/
# （worktree 的 tmp/ 独立、恒为空）→ 每次都误报漏发；改为 state/ 优先、tmp/ 兼容兜底。
# 只查 ET（停盯总结进文件与上方 _us_wrapup_check 同口径）；港股停盯当场闭环、暂不查。
# 功能上线（2026-10-13）之前的交易日无标记属正常（历史记录不追责）。
def _feishu_check():
    try:
        from zoneinfo import ZoneInfo
        _us_now = datetime.datetime.now().astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return
    _d = _us_now.date()
    if not (_us_now.weekday() < 5 and _us_now.hour >= 16):
        _d = _d - datetime.timedelta(days=1)
        while _d.weekday() >= 5:
            _d = _d - datetime.timedelta(days=1)
    if _d < datetime.date(2026, 10, 13):
        return
    _root = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                          "..", "..", "..", ".."))
    _has_summary = False
    for _dir, _name in (("signals", f"{_d.isoformat()}-ET-signals.md"),
                        ("actions", f"{_d.isoformat()}-ET-actions.md")):
        try:
            if "停盯总结" in open(_os.path.join(_root, _dir, _name), encoding="utf-8").read():
                _has_summary = True
                break
        except Exception:
            continue
    if not _has_summary:
        return
    # 标记名 2026-11-06 起带会话标识（`feishu_sent_us_<交易日>_<stag>.txt`，多会话各发各的；
    # 2026-11-11 起 stag 为完整会话 id 去掉连字符，见 T185HKUS），
    # 本检查用 glob 通配兼容带 / 不带后缀两种。**口径保持「任意会话发过即算」而不是按会话收**：
    # 这里查的是「上一个已完结交易日」，多数会话根本不跨那个交易日，按会话收会反复误报催补发；
    # 会话级的「我发没发」由 monitor_guard 的停盯收尾检查（按 sid 匹配 stop_done）承担。
    import glob as _glob
    _markers = []
    for _dir in ("state", "tmp"):
        _markers += _glob.glob(_os.path.join(_root, _dir,
                                             f"feishu_sent_us_{_d.isoformat()}*.txt"))
    if not _markers:
        print(f"📨 飞书汇报提醒：{_d.isoformat()} ET 的停盯总结已写但未见飞书推送记录——按用户规定"
              f"（每次停盯必发业务版）补发：python3 scripts/notify_feishu.py --file <业务版文件> --market us")

_feishu_check()

# 标的池划分状态（2026-08-19 立，TODO「标的池自动划分」）：多会话并行时启动序列内
# 方向研判定出候选池后跑 `python3 scripts/pool_claim.py claim <候选>` 认领（互斥划分，
# 同一标的不落两会话）；此处打印当前全局划分让 AI / 用户启动即见「谁盯哪些」。
# 停盯收尾跑 release --all 释放（monitor_unregister.sh 已联动，但显式跑一次更稳）。
# 2026-08-20 起认领是无条件步骤（不再只「多会话并行时」）——signal / auto 同规，
# 认领表有活认领时不认领会被 monitor_guard 拦（当日港股实录：signal 会话跳过认领、
# 与 auto 会话重复盯一小时）。在场打印提醒见下（决策时刻工具在场层）。
try:
    import subprocess as _sp3
    _r3 = _sp3.run(["python3", _os.path.join(_os.path.dirname(__file__), "pool_claim.py"), "status"],
                   capture_output=True, text=True, timeout=10)
    _out3 = (_r3.stdout or "").strip()
    if _out3:
        print(_out3)
        if "无认领" not in _out3 and "本会话当前无认领" in _out3:
            print("⚠️ 表里有别人的认领、本会话还没认领——采样前先跑 "
                  "`python3 scripts/pool_claim.py claim <候选标的逗号分隔>`，"
                  "否则启动采样段会被 monitor_guard 拦（2026-08-20 立三层互斥）")
except Exception as _e3:
    print(f"🤝 标的池划分状态读取失败（{_e3}）——python3 scripts/pool_claim.py status 手动确认")

# 密采样守护 watcher 自动开启（2026-08-20 立；取代同日 09:58「默认不开启」决定——同日 11 时
# watcher 已删中断警报 + 掩蔽提示、收缩为「空转警报 + 老虎白名单漂移检测」两个低频警告后，
# 用户立：这两个低频警告不扰民，盯盘开始默认开启；2026-08-21 空转警报再删、仅剩白名单漂移
# 检测，默认开启不变）：确保 launchd 项 com.daytrading.monitor-watcher
# 处于 load 状态——已 load 跳过、未 load 自动 launchctl load（幂等，重复跑无副作用）。
# 盘外 watcher 自身会跳过（in_trading_session 判断），故这里无条件确保 load 也无妨。
# 停盯**不联动 unload**（watcher 只剩白名单漂移一个低频提醒、常驻无害；盘外自动静默），用户明确说
# 「关守卫 / unload watcher」时才手动卸载。
def _ensure_watcher():
    """确保两个 launchd 守护项**真的可用**（2026-11-05 立 T157；2026-11-17 重做 T198）。

    2026-11-17 为什么要重做（真实事故）：旧实现只判「label 在 launchctl list 里」→ 打印
    「已在跑」，从不校验 plist 里的脚本路径是否存在。2026-11-09 项目改名
    DayTradingAgent → trading-agent 时只更新了仓库里的 hooks/*.plist 模板，用户级副本没重新
    部署，于是 position-watchdog（每 60s）与 monitor-watcher（每 10s）从那天起持续失败，累计
    9,175 / 54,590 条「can't open file」写进 /tmp，而每场盘前 preflight 都显示「已在跑」。
    直接后果：2026-11-16 美股止损被动成交后 8 分半无人感知，会话在空仓状态下发起移损 →
    fallback 补挂 STP 凭空开出 24 股裸多。

    现在的判据（任一不满足就自动修复 + 重载；修复后仍异常就红字）：
      ① plist 内容必须等于「仓库模板 + 主工作树根路径」——残留旧项目路径、或钉在临时
         worktree 上的路径都会被识别并重写（worktree 一删就是下一次静默失败）；
      ② plist 里每个 ProgramArguments 路径必须真实存在；
      ③ label 必须处于 load 状态；
      ④ position-watchdog 必须在 5 分钟内写过心跳 = 脚本真的被执行过，而不只是「注册着」。
    """
    import subprocess as _sp
    import time as _time
    import re as _re_w

    def _repo_root():
        return _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                              "..", "..", "..", ".."))

    def _stable_root():
        """守护进程指向**主工作树**（main 分支的常驻目录），不钉在临时 worktree 上。"""
        try:
            out = _sp.run(["git", "-C", _repo_root(), "worktree", "list", "--porcelain"],
                          capture_output=True, text=True, timeout=10).stdout
            for ln in out.splitlines():
                if ln.startswith("worktree "):
                    return ln.split(" ", 1)[1].strip()
        except Exception:
            pass
        return _repo_root()

    root = _stable_root()

    def _want_text(tpl_path):
        txt = open(tpl_path, encoding="utf-8").read()
        return txt.replace("/Users/xhq/Developer/trading-agent", root)

    def _plist_paths(txt):
        return [s for s in _re_w.findall(r"<string>([^<]+)</string>", txt) if s.startswith("/")]

    def _uid():
        return _sp.run(["id", "-u"], capture_output=True, text=True).stdout.strip()

    def _relaunch(label, plist):
        """重载 plist：新系统走 bootout/bootstrap（gui/<uid>），旧系统回退 unload/load。"""
        _sp.run(["launchctl", "bootout", f"gui/{_uid()}/{label}"], capture_output=True, text=True)
        _sp.run(["launchctl", "unload", plist], capture_output=True, text=True)
        r = None
        for cmd in (["launchctl", "bootstrap", f"gui/{_uid()}", plist],
                    ["launchctl", "load", plist]):
            r = _sp.run(cmd, capture_output=True, text=True)
            if r.returncode == 0:
                return True, ""
        return False, ((r.stderr or r.stdout).strip() if r else "未知错误")

    def _ensure_one(label, human, heartbeat_secs=None):
        short = label.split(".")[-1]
        tpl = _os.path.join(_repo_root(), "hooks", f"{label}.plist")
        dst = _os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")
        if not _os.path.isfile(tpl):
            print(f"⚠️ {human}: 仓库模板缺失（{tpl}）——无法自愈，请检查仓库")
            return
        want = _want_text(tpl)
        problems = []
        have = None
        if _os.path.isfile(dst):
            try:
                have = open(dst, encoding="utf-8").read()
            except OSError:
                have = None
        else:
            problems.append("plist 未部署")
        if have is not None:
            dead = [p for p in _plist_paths(have) if not _os.path.exists(p)]
            if have.strip() != want.strip():
                problems.append("plist 与仓库模板不一致"
                                + (f"（其中不存在：{dead}）" if dead else ""))
            elif dead:
                problems.append(f"plist 里有不存在的路径 {dead}")
        if problems:
            try:
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(want)
                ok, err = _relaunch(label, dst)
                print(f"🛡️ {human}: 已重写 plist 并重载（{'；'.join(problems)}）"
                      + ("" if ok else f" ⚠️ 重载失败：{err}"))
            except OSError as e:
                print(f"⚠️ {human}: plist 修复失败（{e}）——手动：launchctl load {dst}")
                return
            _time.sleep(2)
        if label not in (_sp.run(["launchctl", "list"], capture_output=True,
                                 text=True).stdout or ""):
            ok, err = _relaunch(label, dst)
            print(f"🛡️ {human}: {'已开启' if ok else '开启失败（' + err + '）'}")
        elif not problems:
            print(f"🛡️ {human}: 已部署、已加载、plist 路径有效")
        if heartbeat_secs:
            beat = _os.path.realpath(_os.path.join(root, "state", "watchdog_beat.json"))

            def _age():
                try:
                    return _time.time() - _os.path.getmtime(beat)
                except OSError:
                    return None

            age = _age()
            if age is None or age > heartbeat_secs:
                # 主动踢一次让它立刻跑，再判（launchd 轮询有延迟，不踢容易误报）
                _sp.run(["launchctl", "kickstart", "-k", f"gui/{_uid()}/{label}"],
                        capture_output=True, text=True)
                _time.sleep(2.5)
                age = _age()
            if age is None:
                print(f"🚨 {human}: 心跳文件不存在——脚本从未被真正执行过！"
                      f"看 /tmp/daytrading-{short}.err")
            elif age > heartbeat_secs:
                print(f"🚨 {human}: 心跳已 {int(age)} 秒未更新（阈值 {heartbeat_secs}s）——"
                      f"进程可能在静默失败，看 /tmp/daytrading-{short}.err")
            else:
                print(f"🛡️ {human}: 心跳正常（{int(age)} 秒前）")

    _ensure_one("com.daytrading.monitor-watcher", "密采样守护 watcher（历史项、已无检查项）")
    _ensure_one("com.daytrading.position-watchdog",
                "持仓看护 watchdog（持仓无人盯 / 疑似被动平仓 / 收盘仍有仓）",
                heartbeat_secs=300)

_ensure_watcher()

# 盯盘会话注册（2026-08-11 立；2026-08-12 收窄为仅港股注册）：把本会话会话 ID
# （pi 宿主注入 PI_SESSION_ID）
# 写入 tmp/monitor_sessions.txt，纳入密采样守护 watcher（launchd 每 10 秒）的守护范围。
# ⚠️ **仅港股盯盘注册、美股不注册**（2026-08-12 用户立）：watcher 的 in_trading_session()
# 只在港股盘中检查（美股时段用户休息不打扰），若美股会话也注册、第二天港股开盘 watcher 仍会
# 检查到它 → jsonl 停更 8 小时 > 阈值 → 一直报。故注册源头按市场收窄：只在港股盘中（含盘前
# 09:30 前 30 分钟预启动阶段，覆盖盘前预热场景）注册，美股 / 夜间 / 盘外一律跳过。
# 停盯时由 trade 停盯流程调 scripts/monitor_unregister.sh 注销（正常停盯不被误报）；
# 即便忘注销，watcher 的死会话自动剔除（jsonl 停更 > 30 分钟）也会兜底清理。
def _register_monitor_session():
    import os as _os
    import datetime as _dt
    _sid = _os.environ.get("PI_SESSION_ID", "")
    if not _sid:
        return  # 非 agent 会话内（如手动跑脚本）→ 跳过
    # 港股盘中判断（与 preflight 主流程的 hk 变量口径一致：周一至周五 09:30-12:00 / 13:00-16:00）。
    # 额外允许 09:00 起注册（盘前 30 分钟，覆盖盘前预热启动 monitor_segment 的场景）。
    _now = _dt.datetime.now()
    if _now.weekday() >= 5:
        print("👀 美股/盘外：watcher 不注册（watcher 仅港股盘中守护、美股时段不打扰用户）")
        return
    _t = _now.time()
    _hk_session = (_dt.time(9, 0) <= _t < _dt.time(12, 0)) or (_dt.time(13, 0) <= _t < _dt.time(16, 0))
    if not _hk_session:
        print("👀 美股/盘外：watcher 不注册（watcher 仅港股盘中守护、美股时段不打扰用户）")
        return
    _root = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "..", ".."))
    _reg = _os.path.join(_root, "tmp", "monitor_sessions.txt")
    # T142（2026-09-09）：重新盯盘 = 撤销本会话当日停盯标记（前一交易日段内停盯后同会话重启盯盘，
    # 标记不撤会让 monitor_guard 判③继续放行「已停盯」、漏拦真断采样）。标记文件按日期命名、
    # 跨日自然失效，此处只处理「同日停盯后同会话重启」的窄场景。
    _stopped = _os.path.join(_root, "tmp", f"stopped_sessions_{_now.strftime('%Y%m%d')}.txt")
    try:
        if _os.path.isfile(_stopped) and _sid in open(_stopped).read().splitlines():
            _lines = [l for l in open(_stopped).read().splitlines() if l != _sid]
            with open(_stopped, "w") as _f:
                _f.write("".join(l + "\n" for l in _lines))
            print("🔄 本会话当日停盯标记已撤销（重新盯盘）")
    except Exception:
        pass  # 标记撤销失败不阻断注册（守卫侧标记判空 / 缺文件均视为未停盯，宁可多提醒）
    try:
        _os.makedirs(_os.path.dirname(_reg), exist_ok=True)
        _exists = _os.path.isfile(_reg) and _sid in open(_reg).read().splitlines()
        if not _exists:
            with open(_reg, "a") as _f:
                _f.write(_sid + "\n")
        print(f"👀 港股盯盘会话已注册（密采样 watcher 守护；停盯时自动注销）")
    except Exception as _e:
        print(f"⚠️ 盯盘会话注册失败（{_e}），watcher 不守护本会话")


_register_monitor_session()
