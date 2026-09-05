#!/usr/bin/env bash
# 在本机安装每日定时任务。默认只打印将要做什么，加 --apply 才真正写入。
#
# 为什么建议在本机跑：EDGAR（Form 4 / 13F / 8-K）会拒绝来自云端数据中心 IP 的请求，
# 家里/公司的网络没有这个问题。国会和新闻两个板块在任何网络下都能跑。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
HOUR="${HOUR:-7}"
MINUTE="${MINUTE:-30}"
APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

VENV="$HERE/.venv"
CMD="$VENV/bin/python -m stock_radar run --config $HERE/config.yaml"
LOG="$HERE/stock-radar.log"

echo "== Stock Radar 定时任务安装"
echo "   目录:     $HERE"
echo "   运行时间: 每个工作日 ${HOUR}:$(printf '%02d' "$MINUTE")（本机时区）"
echo

if [[ ! -d "$VENV" ]]; then
  echo "-- 需要创建虚拟环境: $PYTHON -m venv $VENV && $VENV/bin/pip install -r $HERE/requirements.txt"
else
  echo "-- 虚拟环境已存在: $VENV"
fi

if [[ ! -f "$HERE/config.yaml" ]]; then
  echo "-- 需要生成配置: $VENV/bin/python -m stock_radar init $HERE/config.yaml"
  echo "   生成后请填写 sec.user_agent（SEC 要求带联系邮箱）和 watchlist"
fi

case "$(uname -s)" in
  Darwin) PLATFORM=macos ;;
  *)      PLATFORM=linux ;;
esac

PLIST="$HOME/Library/LaunchAgents/com.stock-radar.daily.plist"
CRON_LINE="$MINUTE $HOUR * * 1-5 cd $HERE && $CMD >> $LOG 2>&1"

if [[ "$PLATFORM" == "macos" ]]; then
  echo "-- macOS：将写入 launchd 任务 $PLIST"
else
  echo "-- Linux：将向 crontab 追加一行"
  echo "   $CRON_LINE"
fi

if [[ "$APPLY" -ne 1 ]]; then
  echo
  echo "以上都还没有执行。确认无误后运行：  bash $0 --apply"
  exit 0
fi

echo
[[ -d "$VENV" ]] || "$PYTHON" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$HERE/requirements.txt"
[[ -f "$HERE/config.yaml" ]] || "$VENV/bin/python" -m stock_radar init "$HERE/config.yaml"

if [[ "$PLATFORM" == "macos" ]]; then
  mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.stock-radar.daily</string>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/python</string><string>-m</string><string>stock_radar</string>
    <string>run</string><string>--config</string><string>$HERE/config.yaml</string>
  </array>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>StartCalendarInterval</key>
  <array>
$(for day in 1 2 3 4 5; do
  printf '    <dict><key>Weekday</key><integer>%s</integer><key>Hour</key><integer>%s</integer><key>Minute</key><integer>%s</integer></dict>\n' "$day" "$HOUR" "$MINUTE"
done)
  </array>
</dict>
</plist>
PLISTEOF
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  echo "✅ 已安装 launchd 任务。查看: launchctl list | grep stock-radar"
  echo "   卸载: launchctl unload $PLIST && rm $PLIST"
else
  if crontab -l 2>/dev/null | grep -qF "stock_radar run"; then
    echo "⚠️  crontab 里已有 stock_radar 任务，未重复添加。手动编辑: crontab -e"
  else
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
    echo "✅ 已写入 crontab。查看: crontab -l"
    echo "   卸载: crontab -e 后删掉这一行"
  fi
fi

echo
echo "先手动跑一次确认没问题：  cd $HERE && $CMD"
