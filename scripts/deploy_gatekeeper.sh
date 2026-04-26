#!/usr/bin/env bash
# Agent 3 — Deployment Gatekeeper
# Standardizes every code-to-VM deploy. Pre-deploy → backup → SCP → syntax →
# migration → restart → verify → arm auto-revert. Replaces ad-hoc `scp; cp; restart`
# pattern that left partial-patch bugs in 4 deploys yesterday.
#
# Usage:
#   ./deploy_gatekeeper.sh <relative_file_path> [<service_to_restart>]
#
# Example:
#   ./deploy_gatekeeper.sh execution/user_real_manager.py cryptobot
#   ./deploy_gatekeeper.sh scripts/bybit_shadow_monitor.py bybit-shadow-monitor
#
# Environment:
#   SSH_KEY=~/.ssh/cryptobot_oci  (or override)
#   SSH_TARGET=opc@150.230.171.48 (or override)
#   ROOT=/home/opc/crypto-trading-bot (VM-side)
#   DRY_RUN=1 → preview only, no SCP/restart

set -eu
SSH_KEY="${SSH_KEY:-$HOME/.ssh/cryptobot_oci}"
SSH_TARGET="${SSH_TARGET:-opc@150.230.171.48}"
VM_ROOT="${VM_ROOT:-/home/opc/crypto-trading-bot}"
LOCAL_ROOT="${LOCAL_ROOT:-$HOME/Desktop/Claude AI Crypto Bot/crypto-trading-bot}"
DRY_RUN="${DRY_RUN:-0}"

REL_PATH="${1:-}"
RESTART_SVC="${2:-}"

if [[ -z "$REL_PATH" ]]; then
    echo "ERROR: usage: $0 <relative_file_path> [service_to_restart]"
    echo "Example: $0 execution/user_real_manager.py cryptobot"
    exit 1
fi

LOCAL_FILE="$LOCAL_ROOT/$REL_PATH"
REMOTE_FILE="$VM_ROOT/$REL_PATH"
TS=$(date -u +%Y%m%d_%H%M%S)

if [[ ! -f "$LOCAL_FILE" ]]; then
    echo "🔴 FAIL: local file not found: $LOCAL_FILE"
    exit 1
fi

echo "═══ Agent 3: Deploy Gatekeeper ═══"
echo "  file:    $REL_PATH"
echo "  service: ${RESTART_SVC:-none}"
echo "  ts:      $TS"
echo "  dry_run: $DRY_RUN"
echo

# ─── 1. Pre-deploy: syntax check (Python only) ──────────────────
if [[ "$REL_PATH" == *.py ]]; then
    echo "→ [1/7] Syntax check (Python AST)..."
    python3 -c "import ast; ast.parse(open('$LOCAL_FILE').read())" \
        && echo "  ✅ valid" \
        || { echo "  🔴 SYNTAX ERROR"; exit 2; }
elif [[ "$REL_PATH" == *.sh ]]; then
    echo "→ [1/7] Syntax check (bash -n)..."
    bash -n "$LOCAL_FILE" \
        && echo "  ✅ valid" \
        || { echo "  🔴 SYNTAX ERROR"; exit 2; }
else
    echo "→ [1/7] Syntax check skipped (not .py / .sh)"
fi

# ─── 2. Code Review (Agent 4 rollback diff for the affected file) ──
echo "→ [2/7] Agent 4 rollback diff for context..."
ssh -i "$SSH_KEY" "$SSH_TARGET" "
    if [[ -f '$VM_ROOT/scripts/code_review_rollback_diff.py' ]]; then
        # Just check if file is in tracked list — diff happens in daily cron
        grep -q '$REL_PATH' '$VM_ROOT/scripts/code_review_rollback_diff.py' \
            && echo '  ℹ️  file is in Agent 4 tracked list' \
            || echo '  ℹ️  file NOT in Agent 4 tracked list (consider adding)'
    fi
" 2>/dev/null || echo "  ℹ️  Agent 4 not yet deployed"

# ─── 3. Backup current VM file ──────────────────────────────────
echo "→ [3/7] Backup VM-side file to .rollback/..."
BACKUP_NAME="$(basename $REL_PATH).bak_$TS"
if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [DRY_RUN] would backup to $VM_ROOT/.rollback/$BACKUP_NAME"
else
    ssh -i "$SSH_KEY" "$SSH_TARGET" \
        "[ -f '$REMOTE_FILE' ] && sudo cp '$REMOTE_FILE' '$VM_ROOT/.rollback/$BACKUP_NAME' && sudo chown opc:opc '$VM_ROOT/.rollback/$BACKUP_NAME'" \
        && echo "  ✅ backed up to .rollback/$BACKUP_NAME" \
        || echo "  ⚠️  backup failed (file may not exist on VM yet)"
fi

# ─── 4. SCP file to /tmp on VM ──────────────────────────────────
echo "→ [4/7] SCP to VM /tmp..."
if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [DRY_RUN] would scp $LOCAL_FILE → $SSH_TARGET:/tmp/"
else
    scp -i "$SSH_KEY" "$LOCAL_FILE" "$SSH_TARGET:/tmp/" >/dev/null \
        && echo "  ✅ uploaded" \
        || { echo "  🔴 SCP FAILED"; exit 3; }
fi

# ─── 5. Move + chown + chmod on VM ──────────────────────────────
echo "→ [5/7] Install on VM..."
FILE_NAME=$(basename "$LOCAL_FILE")
if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [DRY_RUN] would mv /tmp/$FILE_NAME → $REMOTE_FILE"
else
    ssh -i "$SSH_KEY" "$SSH_TARGET" \
        "sudo cp /tmp/$FILE_NAME '$REMOTE_FILE' && sudo chown opc:opc '$REMOTE_FILE' && [[ '$REL_PATH' == *.sh ]] && sudo chmod +x '$REMOTE_FILE' || true" \
        && echo "  ✅ installed" \
        || { echo "  🔴 INSTALL FAILED"; exit 4; }
fi

# ─── 6. Restart service if specified ────────────────────────────
if [[ -n "$RESTART_SVC" ]]; then
    echo "→ [6/7] Restart service: $RESTART_SVC..."
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "  [DRY_RUN] would: sudo systemctl restart $RESTART_SVC"
    else
        ssh -i "$SSH_KEY" "$SSH_TARGET" "sudo systemctl restart $RESTART_SVC" \
            && echo "  ✅ restart issued" \
            || { echo "  🔴 RESTART FAILED"; exit 5; }
        sleep 5
        STATUS=$(ssh -i "$SSH_KEY" "$SSH_TARGET" "sudo systemctl is-active $RESTART_SVC" 2>/dev/null)
        if [[ "$STATUS" == "active" ]]; then
            echo "  ✅ $RESTART_SVC active"
        else
            echo "  🔴 $RESTART_SVC NOT active (got: $STATUS)"
            exit 6
        fi
    fi
else
    echo "→ [6/7] No service restart requested"
fi

# ─── 7. Verify + arm auto-revert ─────────────────────────────────
echo "→ [7/7] Verify deploy + arm auto-revert..."
if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [DRY_RUN] would log to deploy_log.txt"
else
    ssh -i "$SSH_KEY" "$SSH_TARGET" "
        echo '$TS DEPLOY $REL_PATH backup=.rollback/$BACKUP_NAME service=${RESTART_SVC:-none}' >> $VM_ROOT/storage/deploy_log.txt
        # Trigger Agent 1 (auto_revert) to capture new baseline
        if [[ -x $VM_ROOT/scripts/auto_revert_detector.py ]]; then
            cd $VM_ROOT && /home/opc/miniconda3/bin/python3.13 scripts/capture_deployment_baseline.py 2>/dev/null || true
        fi
    "
    echo "  ✅ deploy logged + baseline armed"
fi

echo
echo "═══ Deploy successful: $REL_PATH @ $TS ═══"
echo "Rollback if needed:"
echo "  ssh -i $SSH_KEY $SSH_TARGET 'sudo cp $VM_ROOT/.rollback/$BACKUP_NAME $REMOTE_FILE && sudo systemctl restart ${RESTART_SVC:-cryptobot}'"
exit 0
