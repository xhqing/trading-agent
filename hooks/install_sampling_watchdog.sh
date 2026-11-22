#!/usr/bin/env bash
# 安装 / 卸载「盯盘中断外部看护」定时任务（2026-11-22 立，T202HKUS）。
#
# 把 plist 模板里写死的 Python 路径与项目根路径，换成**当前这台机器上实际的值**
# （MacBook 是 /opt/homebrew/bin/python3 与 /Users/xhq/...，macmini 是 /usr/local/bin/python3
# 与 /Users/mac/...），再加载进当前用户的 launchd 域。重复执行安全（先卸载同名服务再装）。
#
#   bash hooks/install_sampling_watchdog.sh             # 安装（或更新）并立即启用
#   bash hooks/install_sampling_watchdog.sh --unload    # 卸载（plist 文件保留，便于恢复）
#
# 正常情况下不需要手工跑——`preflight.py` 的 `_ensure_watcher()` 每次盯盘启动都会自动部署、
# 自愈（plist 内容与模板不一致 / 路径失效 / 未加载都会修），并校验心跳
# `state/sampling_watchdog_beat.json` 是否新鲜。本脚本用于一次性手工安装与排障。

set -euo pipefail

LABEL="com.daytrading.sampling-watchdog"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/hooks/${LABEL}.plist"
DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--unload" ]]; then
    launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || launchctl unload -w "$DST" 2>/dev/null || true
    echo "✅ 已卸载 ${LABEL}（plist 保留在 ${DST}）"
    exit 0
fi

if [[ ! -f "$SRC" ]]; then
    echo "⛔ 找不到 plist 模板：${SRC}" >&2
    exit 1
fi
PY="$(command -v python3)"
if [[ -z "$PY" ]]; then
    echo "⛔ 找不到 python3" >&2
    exit 1
fi

mkdir -p "$(dirname "$DST")"
python3 - "$SRC" "$DST" "$PY" "$ROOT" <<'PY'
import sys
src, dst, py, root = sys.argv[1:5]
with open(src, encoding="utf-8") as fh:
    text = fh.read()
text = text.replace("/opt/homebrew/bin/python3", py)
text = text.replace("/Users/xhq/Developer/trading-agent", root)
with open(dst, "w", encoding="utf-8") as fh:
    fh.write(text)
print(f"✅ 已生成 {dst}（python={py}，项目根={root}）")
PY

plutil -lint "$DST"

launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
if ! launchctl bootstrap "$DOMAIN" "$DST" 2>/dev/null; then
    launchctl load -w "$DST"
fi

if launchctl list | grep -q "$LABEL"; then
    echo "✅ ${LABEL} 已启用（每 60 秒巡检一次；RunAtLoad 已立即跑一次）"
    echo "   当前判定：python3 ${ROOT}/hooks/sampling_watchdog.py --status"
    echo "   日志：/tmp/daytrading-sampling-watchdog.log 与 .err"
else
    echo "⚠️ 未在 launchctl list 里看到 ${LABEL}，请手工确认：launchctl bootstrap ${DOMAIN} ${DST}" >&2
    exit 1
fi
