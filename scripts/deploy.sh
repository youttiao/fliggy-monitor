#!/usr/bin/env bash
# 本地 deploy 辅助脚本：手动 rsync + 重启（不走 GitHub Actions）。
#
# 适用场景：
#   - 第一次部署（仓还没建 / Actions 还没配）
#   - Actions 失败的紧急修复
#   - CI 暂不可用时的手工 fallback
#
# 用法：
#   VPS_HOST=107.172.144.102 VPS_USER=monitor VPS_SSH_KEY=~/.ssh/id_ed25519 \
#     ./scripts/deploy.sh
#
# 必需环境变量：
#   VPS_HOST      VPS 公网 IP / 域名
#   VPS_USER      部署用户（建议 monitor，非 root）
#   VPS_SSH_KEY   私钥路径
#   VPS_PORT      可选，默认 22

set -euo pipefail

VPS_HOST="${VPS_HOST:-107.172.144.102}"
VPS_USER="${VPS_USER:-monitor}"
VPS_PORT="${VPS_PORT:-22}"
VPS_SSH_KEY="${VPS_SSH_KEY:-$HOME/.ssh/id_fliggy-monitor}"
REMOTE_DIR="${REMOTE_DIR:-/opt/fliggy-monitor}"

echo "=== deploy target ==="
echo "  host=$VPS_HOST  user=$VPS_USER  port=$VPS_PORT  remote=$REMOTE_DIR"
echo "  ssh_key=$VPS_SSH_KEY"
[ -f "$VPS_SSH_KEY" ] || { echo "ERROR: SSH 私钥不存在 $VPS_SSH_KEY"; exit 1; }

SSH_OPTS=(-i "$VPS_SSH_KEY" -p "$VPS_PORT" -o StrictHostKeyChecking=accept-new)

echo "=== step 1: rsync (exclude cookies.json, .db, .venv) ==="
rsync -avz --delete \
  -e "ssh ${SSH_OPTS[*]}" \
  --exclude='.git' \
  --exclude='*.db' --exclude='backups/*.db' \
  --exclude='cookies.json' \
  --exclude='__pycache__' --exclude='.venv' --exclude='.cache' \
  --exclude='screenshots' \
  ./ "${VPS_USER}@${VPS_HOST}:${REMOTE_DIR}/"

echo "=== step 2: remote install + restart ==="
ssh "${SSH_OPTS[@]}" "${VPS_USER}@${VPS_HOST}" << 'REMOTE'
  set -e
  cd /opt/fliggy-monitor
  if [ ! -d .venv ]; then
    sudo -u monitor /usr/bin/python3 -m venv .venv
  fi
  sudo -u monitor .venv/bin/pip install --upgrade pip -q
  sudo -u monitor .venv/bin/pip install -r requirements.txt -q

  # 构建 Chrome 扩展 zip（web 路由会读取 dist/fliggy-cookie-sync.zip）
  bash extensions/fliggy-cookie-sync/build.sh

  # 幂等迁移：新加的表/索引都靠 IF NOT EXISTS 兜底
  sudo -u monitor /opt/fliggy-monitor/.venv/bin/python3 /opt/fliggy-monitor/scripts/init_db.py --no-baseline || true

  if [ -f /etc/fliggy-monitor/cookies.json ]; then
    echo "cookies.json preserved at /etc/fliggy-monitor/cookies.json"
  else
    echo "WARNING: /etc/fliggy-monitor/cookies.json missing"
  fi

  sudo /usr/bin/systemctl restart fliggy-web || true
  sudo /usr/bin/systemctl restart fliggy-cookies.timer || true
  sleep 3
  sudo /usr/bin/systemctl status fliggy-web --no-pager | head -10 || true
  echo "=== deploy done: $(date -Iseconds) ==="
REMOTE

echo "=== step 3: health check ==="
ssh "${SSH_OPTS[@]}" "${VPS_USER}@${VPS_HOST}" \
  "curl -fsS http://127.0.0.1:8080/healthz && echo OK || echo FAIL"

echo "=== done ==="