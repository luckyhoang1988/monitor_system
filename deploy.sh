#!/usr/bin/env bash
#
# deploy.sh — Deploy Monitor System lên production server.
#
# Quy trình (theo .cursor/rules/deploy-workflow.mdc):
#   1. Push branch master lên GitHub (origin)
#   2. SSH vào server (alias monitorsrv → 10.0.193.234) pull code mới
#   3. Rebuild + restart các service Docker: app, worker, beat
#   4. Reload nginx, in trạng thái container
#
# Cách dùng:
#   ./deploy.sh            # push + deploy
#   ./deploy.sh --no-push  # bỏ qua push (code đã push sẵn), chỉ deploy trên server
#
set -euo pipefail

SSH_HOST="monitorsrv"
REMOTE_DIR="/home/monitorsys/monitor_system"
BRANCH="master"

PUSH=1
if [[ "${1:-}" == "--no-push" ]]; then
    PUSH=0
fi

echo "==> [1/3] Đồng bộ code"
if [[ "$PUSH" -eq 1 ]]; then
    git push origin "$BRANCH"
else
    echo "    (bỏ qua git push — dùng code đã push sẵn)"
fi

echo "==> [2/3] Pull + rebuild + restart trên $SSH_HOST"
ssh "$SSH_HOST" "set -e \
  && cd $REMOTE_DIR \
  && git pull origin $BRANCH \
  && docker compose up -d --build app worker beat \
  && docker compose restart nginx"

echo "==> [3/3] Trạng thái container"
ssh "$SSH_HOST" "cd $REMOTE_DIR && docker compose ps"

echo "==> Deploy hoàn tất."
