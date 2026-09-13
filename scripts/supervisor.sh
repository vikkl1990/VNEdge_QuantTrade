#!/usr/bin/env bash
# Process supervision for the three long-running services (bot, ML Lab
# server, ML auto-retrainer), via macOS launchd instead of bare `nohup ... &`.
#
# (2026-09-13) Every one of these has been running unsupervised all session:
# no auto-restart on crash, nothing paging anyone if it dies. launchd
# KeepAlive relaunches within ThrottleInterval seconds of any exit, for any
# reason, and RunAtLoad brings all three back up after a reboot or logout —
# without needing this session, or anyone, to remember to run a script.
#
#   scripts/supervisor.sh install     # copy plists to ~/Library/LaunchAgents, bootstrap all 3
#   scripts/supervisor.sh uninstall   # bootout all 3, remove the copied plists
#   scripts/supervisor.sh status      # launchctl print for each
#   scripts/supervisor.sh restart     # kickstart -k (forces an immediate respawn) for each
#   scripts/supervisor.sh logs <bot|mllab|retrainer>
#
# Each service's own single-instance guard (main.py's .bot.pid lock; ML Lab's
# port-ownership check) still applies underneath launchd, so a manual
# scripts/run_bot.sh or scripts/ml_lab.sh invocation while supervision is
# installed will correctly refuse to start a duplicate rather than fight it
# for the ledger or the port.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_SRC="$ROOT/scripts/launchd"
PLIST_DST="$HOME/Library/LaunchAgents"
LABELS=(com.vnedge.bot com.vnedge.mllab com.vnedge.retrainer)
UID_DOMAIN="gui/$(id -u)"

install() {
  mkdir -p "$PLIST_DST" "$ROOT/logs"
  for label in "${LABELS[@]}"; do
    cp "$PLIST_SRC/$label.plist" "$PLIST_DST/$label.plist"
    launchctl bootout "$UID_DOMAIN/$label" 2>/dev/null || true
    launchctl bootstrap "$UID_DOMAIN" "$PLIST_DST/$label.plist"
    launchctl enable "$UID_DOMAIN/$label"
    echo "installed + started: $label"
  done
  echo
  echo "Supervision installed. These 3 services now auto-restart on crash and"
  echo "auto-start at login. Check with: scripts/supervisor.sh status"
}

uninstall() {
  for label in "${LABELS[@]}"; do
    launchctl bootout "$UID_DOMAIN/$label" 2>/dev/null || true
    rm -f "$PLIST_DST/$label.plist"
    echo "removed: $label"
  done
  echo
  echo "Supervision removed. Processes already running keep running until"
  echo "stopped by hand; nothing will auto-restart them or relaunch at login."
}

status() {
  for label in "${LABELS[@]}"; do
    echo "── $label ──"
    launchctl print "$UID_DOMAIN/$label" 2>/dev/null \
      | grep -E "state|pid|last exit" || echo "  not loaded"
    echo
  done
}

restart() {
  for label in "${LABELS[@]}"; do
    launchctl kickstart -k "$UID_DOMAIN/$label" 2>/dev/null \
      && echo "restarted: $label" || echo "not loaded: $label"
  done
}

logs() {
  case "${1:-}" in
    bot) tail -f "$ROOT/logs/bot.log" ;;
    mllab) tail -f "$ROOT/logs/ml_lab.log" ;;
    retrainer) tail -f "$ROOT/logs/ml_retrain.log" ;;
    *) echo "usage: $0 logs {bot|mllab|retrainer}"; exit 1 ;;
  esac
}

case "${1:-status}" in
  install) install ;;
  uninstall) uninstall ;;
  status) status ;;
  restart) restart ;;
  logs) shift; logs "$@" ;;
  *) echo "usage: $0 {install|uninstall|status|restart|logs}"; exit 1 ;;
esac
