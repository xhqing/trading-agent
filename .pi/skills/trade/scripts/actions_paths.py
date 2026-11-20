#!/usr/bin/env python3
"""actions 记录文件的「分会话」路径规则（2026-11-20 立，T203HKUS）。

## 为什么改

原来一天一个市场只有**一个** `actions/<date>-<market>-actions.md`，同一天同一市场的
多个盯盘会话全写同一份文件（worktree 按「市场 + 交易日」共享，见 SKILL.md）。后果：

1. **提交时序打架**：A 会话 commit 时只快照了当时内容，B 会话随后继续追加，工作区
   立刻又变 dirty——2026-11-19 美股场次实录：#151 提交后 `actions/2026-11-19-ET-actions.md`
   又出现未提交改动（B 会话后续写的动作记录与停盯总结），最后由 B 自己的 #152 入库。
2. **持仓推导串台**：`account_status.py` 从当日文件里推导「本会话是否持仓」，会把别的
   会话的开仓记录当成自己的（2026-11-19 实录：本会话空仓，采样段却每 40 秒报一条
   「AMD 36 股 与 actions 记录不一致」硬告警）。

## 规则

- **写**：`actions/<date>-<market>-actions-<stag>.md`（`stag` = `session_tag.session_tag()`，
  完整会话 id 去连字符的 32 位十六进制）。拿不到 `PI_SESSION_ID`（人工终端手跑）时
  退回旧名 `actions/<date>-<market>-actions.md`，保证手工场景不改行为。
- **读（本会话口径）**：`own_or_legacy()` —— 本会话当天的分文件存在就只读它；不存在
  则退回旧单文件（兼容历史会话与手工记录）。
- **读（该日全量口径）**：`day_files()` —— 旧单文件 + 该日该市场所有分文件，用于
  「按成交价匹配原始止损」「该日是否已写停盯总结」这类跨会话查询。
- **读（历史全量）**：`all_files()` —— 供 r_audit / ev_dual_report / actions_check 之类
  的全库扫描；`parse_name()` 解析文件名（旧名返回 `stag=None`）。

## 边界

- 本模块只负责**路径**，不碰内容、不判断会话归属（内容里的 `🆔 会话` 标记仍由
  `session_tag.session_matches()` 判断）。
- 旧文件永不改名、永不迁移：历史记录保持原样，读侧同时兼容两种命名。
"""

import glob
import os
import re

from session_tag import session_tag

# 市场代码 → 文件名里的后缀
SUFFIX = {"hk": "HKT", "us": "ET"}
MARKET_BY_SUFFIX = {"HKT": "hk", "ET": "us"}

# <date>-<SUFFIX>-actions[-<stag>].md
_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(HKT|ET)-actions(?:-([0-9A-Za-z]+))?\.md$")


def actions_dir(root=None):
    """actions 目录绝对路径。root 缺省 = 本文件所在的 skill 上溯到项目根。"""
    if root is None:
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.abspath(os.path.join(here, "..", "..", "..", ".."))
    return os.path.join(root, "actions")


def parse_name(name):
    """解析 actions 文件名 → {'date','market','stag','legacy'}；不匹配返回 None。

    `stag=None` / `legacy=True` 表示旧版「一天一个市场一个文件」的命名。
    """
    m = _NAME_RE.match(os.path.basename(name))
    if not m:
        return None
    return {
        "date": m.group(1),
        "market": MARKET_BY_SUFFIX[m.group(2)],
        "stag": m.group(3),
        "legacy": m.group(3) is None,
    }


def _fname(date, market, stag=None):
    suffix = SUFFIX[market]
    return f"{date}-{suffix}-actions-{stag}.md" if stag else f"{date}-{suffix}-actions.md"


def write_path(market, date, root=None, sid=None):
    """本会话该日该市场的写入目标（分文件）；拿不到会话标识时退回旧名。"""
    stag = session_tag(sid)
    return os.path.join(actions_dir(root), _fname(date, market, stag or None))


def own_files(market, date, root=None, sid=None):
    """本会话该日该市场的文件列表（至多一个；不存在返回空列表）。"""
    p = write_path(market, date, root, sid)
    return [p] if os.path.exists(p) else []


def day_files(market, date, root=None):
    """该日该市场的全部动作文件（旧单文件 + 所有分文件），按 mtime 升序。

    用于「跨会话」查询：按成交价匹配原始止损、判断该日是否已写停盯总结等。
    """
    out = []
    for fp in glob.glob(os.path.join(actions_dir(root), "*-actions*.md")):
        info = parse_name(fp)
        if info and info["date"] == date and info["market"] == market:
            out.append(fp)
    out.sort(key=lambda p: (os.path.getmtime(p), p))
    return out


def own_or_legacy(market, date, root=None, sid=None):
    """本会话口径的读取集合：本会话分文件存在就只读它；否则退回旧单文件（兼容）。

    同一天同一市场并行多个会话时，本函数保证**不会读到别的会话的记录**——这正是
    「持仓推导串台」的修法。
    """
    own = own_files(market, date, root, sid)
    if own:
        return own
    legacy = os.path.join(actions_dir(root), _fname(date, market))
    return [legacy] if os.path.exists(legacy) else []


def all_files(root=None, market=None):
    """全部历史动作文件（旧单文件 + 各会话分文件），按 mtime 升序。"""
    out = []
    for fp in glob.glob(os.path.join(actions_dir(root), "*-actions*.md")):
        info = parse_name(fp)
        if not info:
            continue
        if market and info["market"] != market:
            continue
        out.append(fp)
    out.sort(key=lambda p: (os.path.getmtime(p), p))
    return out


def has_summary(files, marker="停盯总结"):
    """给定文件列表里是否已写过停盯总结（用于收尾判定，跨会话全量口径）。"""
    for fp in files:
        try:
            if marker in open(fp, encoding="utf-8").read():
                return True
        except Exception:
            continue
    return False
