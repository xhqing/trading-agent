#!/usr/bin/env python3
"""复盘数据变更点（change tag）维护与读取（2026-11-23 用户立，T209HKUS）。

**为什么需要**：用户原话——「重大改动可能改变交易样本的分布」，所以每次重大改动都要在
**复盘数据的时间轴上打一个戳**，复盘时重点提醒：跨这些戳的统计不能直接合并比较，必须先
看分期间对比表再下结论。上一条 tag 由用户标定为 2026-10-11（具体改动待确认）。

**什么算重大改动**（写进 `affects`，可多选）：
  - `sample_selection`：改变「哪些单会成交 / 被拦」——门槛、护栏闸、费率口径等
  - `r_denominator`：R 的分母（净 max_loss）口径变化
  - `r_numerator`：R 的分子（盈亏 / 费用）口径变化
  - `position_size`：单笔仓位规模变化（B / 买力 / 降档规则）
  - `inclusion`：样本纳入范围变化（异常样本排除、补记规则等）
  - `monitoring`：盯盘连续性变化（采样断链、看护、会话复活 → 漏单多少）
  - `decision_source`：决策产出者变化（换模型、换宿主、prompt/规则大改 → 判断风格可能变）
     —— 2026-11-23 补：用户指出 10-11 起主力模型改用 deepseek-flash，这类改动不改变
     门槛/口径，却会改变「AI 怎么判断入场与平仓」，同样是样本分布的一个维度

数据文件：项目根 `reviews/change-tags.json`（入库、可追溯）。复盘脚本
（`review.py` / `auto_review.py`）读它并在报告里强制显示提醒块。

用法：
    python3 change_tags.py                       # 列出全部变更点（表格）
    python3 change_tags.py --json                # 机器可读
    python3 change_tags.py add --date 2026-11-22 \
        --title "盯盘断链兜底修复 + 会话外看护 + auto 会话复活" \
        --affects monitoring --scope US \
        --why "修掉两个 bug 后兜底恢复生效，新增看护与复活；影响样本完整性" \
        --evidence "CHANGELOG 0.12.1–0.12.4"
    python3 change_tags.py check                 # 校验文件（日期升序 / 取值合法 / 无重复）

边界（诚实）：**「哪些改动算重大」是人的判断，无法机械化**——所以本工具只保证「登记了
就一定被复盘看见」，不保证「该登记的都被登记」。判断标准是「它会不会让改动前后的样本不
可比」；拿不准就登记（多打一个戳的代价远小于把两段不同口径的样本混在一起算）。
"""
import argparse
import json
import os
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "..", ".."))
DEFAULT_PATH = os.path.join(PROJECT_ROOT, "reviews", "change-tags.json")

AFFECTS = {
    "sample_selection": "样本选择（哪些单会成交/被拦）",
    "r_denominator": "R 分母（净 max_loss）口径",
    "r_numerator": "R 分子（盈亏/费用）口径",
    "position_size": "单笔仓位规模（B/买力/降档）",
    "inclusion": "样本纳入范围（异常样本/补记）",
    "monitoring": "盯盘连续性（断链/看护/复活 → 漏单）",
    "decision_source": "决策来源（模型 / 宿主 / prompt 变更）",
}


def load_tags(path=None):
    """读变更点（按日期升序）。文件缺失 / 损坏 → 空列表（不阻断复盘）。"""
    path = path or DEFAULT_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    tags = data.get("tags") if isinstance(data, dict) else None
    if not isinstance(tags, list):
        return []
    out = [t for t in tags if isinstance(t, dict) and t.get("date")]
    out.sort(key=lambda t: t["date"])
    return out


def affects_label(keys):
    return "、".join(AFFECTS.get(k, k) for k in (keys or []))


def period_index(date_str, tags):
    """某个交易日属于第几个期间（0 = 第一个 tag 之前；i = 第 i 个 tag 之后）。"""
    idx = 0
    for i, t in enumerate(tags, start=1):
        if date_str >= t["date"]:
            idx = i
    return idx


def period_label(idx, tags):
    if idx == 0:
        return f"tag 之前（{tags[0]['date']} 前）" if tags else "无 tag"
    t = tags[idx - 1]
    end = tags[idx]["date"] if idx < len(tags) else "至今"
    return f"{t['date']} ~ {end}"


def _save(tags, path=None):
    path = path or DEFAULT_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    data.setdefault("_comment", "复盘数据变更点（change tag）——见 change_tags.py docstring")
    data["tags"] = tags
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _validate(tags):
    problems = []
    seen = set()
    prev = ""
    for t in tags:
        d = t.get("date", "")
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            problems.append(f"日期格式不对：{d!r}（应 YYYY-MM-DD）")
        if d in seen:
            problems.append(f"日期重复：{d}")
        seen.add(d)
        if prev and d < prev:
            problems.append(f"日期未升序：{d} 在 {prev} 之后")
        prev = d
        for k in t.get("affects") or []:
            if k not in AFFECTS:
                problems.append(f"{d}：affects 取值非法 {k!r}（合法：{', '.join(AFFECTS)}）")
        if not t.get("title"):
            problems.append(f"{d}：缺 title")
        for mk in t.get("scope") or []:
            if mk not in ("HK", "US"):
                problems.append(f"{d}：scope 取值非法 {mk!r}（只能是 HK / US）")
    return problems


def main():
    ap = argparse.ArgumentParser(description="复盘数据变更点（change tag）维护")
    ap.add_argument("cmd", nargs="?", default="list", choices=["list", "add", "check"])
    ap.add_argument("--path", help="变更点文件（默认 reviews/change-tags.json）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--date")
    ap.add_argument("--title")
    ap.add_argument("--affects", help="逗号分隔，取值见 docstring")
    ap.add_argument("--scope", default="HK,US", help="逗号分隔（HK / US），默认两者")
    ap.add_argument("--why")
    ap.add_argument("--evidence")
    ap.add_argument("--version")
    ap.add_argument("--unverified", action="store_true", help="标记为待确认（source=user）")
    args = ap.parse_args()

    path = args.path or DEFAULT_PATH
    tags = load_tags(path)

    if args.cmd == "list":
        if args.json:
            print(json.dumps(tags, ensure_ascii=False, indent=2))
            return 0
        if not tags:
            print("（还没有任何变更点；用 change_tags.py add 登记）")
            return 0
        print(f"复盘数据变更点：共 {len(tags)} 个（{path}）")
        print(f"{'date':<12}{'影响':<40}{'范围':<8}标题")
        for t in tags:
            flag = "" if t.get("verified", True) else "（待确认）"
            print(f"{t['date']:<12}{affects_label(t.get('affects'))[:38]:<40}"
                  f"{'/'.join(t.get('scope') or []):<8}{t.get('title', '')}{flag}")
        newest = tags[-1]
        print(f"\n最近一个变更点：{newest['date']}｜{newest.get('title', '')}")
        return 0

    if args.cmd == "check":
        problems = _validate(tags)
        if problems:
            print("❌ 变更点文件有问题：")
            for p in problems:
                print(f"  - {p}")
            return 1
        print(f"✅ 变更点文件正常：{len(tags)} 个（日期升序、取值合法、无重复）")
        return 0

    # add
    missing = [k for k in ("date", "title", "affects", "why") if not getattr(args, k)]
    if missing:
        sys.exit(f"❌ add 缺参数：{'/'.join('--' + m for m in missing)}（用法见 --help）")
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        sys.exit(f"❌ --date 格式不对：{args.date!r}（应 YYYY-MM-DD）")
    affects = [a.strip() for a in args.affects.split(",") if a.strip()]
    bad = [a for a in affects if a not in AFFECTS]
    if bad:
        sys.exit(f"❌ --affects 有非法取值 {bad}（合法：{', '.join(AFFECTS)}）")
    scope = [s.strip().upper() for s in args.scope.split(",") if s.strip()]
    if any(s not in ("HK", "US") for s in scope):
        sys.exit("❌ --scope 只能是 HK / US（逗号分隔）")

    entry = {"date": args.date, "title": args.title, "scope": scope,
             "affects": affects, "why": args.why}
    if args.evidence:
        entry["evidence"] = args.evidence
    if args.version:
        entry["version"] = args.version
    entry["source"] = "user" if args.unverified else "manual"
    entry["verified"] = not args.unverified

    merged = {t["date"]: t for t in tags}
    if args.date in merged:
        print(f"⚠️ {args.date} 已有变更点，改为覆盖其内容（原：{merged[args.date].get('title')}）")
    merged[args.date] = entry
    new_tags = [merged[d] for d in sorted(merged)]
    problems = _validate(new_tags)
    if problems:
        sys.exit("❌ 写入后文件不合法：" + "；".join(problems))
    _save(new_tags, path)
    print(f"✅ 已登记变更点 {args.date}：{args.title}｜影响：{affects_label(affects)}")
    print("   复盘脚本会在报告里强制显示这个变更点提醒（review.py / auto_review.py）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
