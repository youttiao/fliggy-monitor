# 08 · GitHub Actions 部署（私有仓 + 自动 Deploy）

> 把代码托管在 GitHub 私有仓，`main` 分支每次推送触发 lint + test + build + deploy_production（手动确认）→ 自动 SSH 到 107.172.144.102 → 拉取最新 → 重启服务。

---

## 8.1 前置假设

| 项 | 假定 | 备注 |
|---|---|---|
| GitHub 仓 | 私有仓 | 用户在 GitHub UI 创建 |
| Runner | GitHub-hosted（ubuntu-latest） | 默认免费额度足够 |
| SSH key | ed25519 密钥对 | 在本地生成 |
| VPS 用户 | `monitor`（非 root） | 见 [05 §5.3](05-deployment-vps.md) |

**默认仓路径**：`github.com/666-XCJ/fliggy-monitor`（与本地目录 `/Users/argo/666-XCJ/fliggy-monitor` 对齐）。需要换 owner / 名字请编辑 `.github/workflows/deploy.yml` 与本文。

---

## 8.2 一次性准备（VPS 端）

```bash
ssh root@107.172.144.102 << 'EOF'
set -e

# 1. monitor 用户已在 05 部署时建好（uid 1001, nologin, home=/home/monitor）

# 2. 给 monitor 装 .ssh + authorized_keys
sudo -u monitor mkdir -p /home/monitor/.ssh
sudo -u monitor chmod 700 /home/monitor/.ssh

# 3. GitHub Runner 公钥（先用本地生成的 id_ed25519.pub）
sudo -u monitor tee /home/monitor/.ssh/authorized_keys << 'KEY'
ssh-ed25519 AAAA...your-public-key... github-actions@fliggy-monitor
KEY
sudo -u monitor chmod 600 /home/monitor/.ssh/authorized_keys

# 4. 限定 monitor 的 sudo 权限（只能 systemctl 操作 fliggy-* 服务）
cat > /etc/sudoers.d/monitor-systemctl << 'SUDO'
monitor ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart fliggy-*
monitor ALL=(ALL) NOPASSWD: /usr/bin/systemctl start   fliggy-*
monitor ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop    fliggy-*
monitor ALL=(ALL) NOPASSWD: /usr/bin/systemctl status  fliggy-*
monitor ALL=(ALL) NOPASSWD: /usr/bin/journalctl -u fliggy-* -n *
monitor ALL=(ALL) NOPASSWD: /usr/bin/cp /etc/fliggy-monitor/cookies.json /etc/fliggy-monitor/cookies.json.bak
Defaults!/usr/bin/systemctl !requiretty
SUDO
chmod 440 /etc/sudoers.d/monitor-systemctl
visudo -c -f /etc/sudoers.d/monitor-systemctl

# 5. /opt/fliggy-monitor 让 monitor 可写
chown -R monitor:monitor /opt/fliggy-monitor
chmod -R u+rwX,g+rX,o-rwx /opt/fliggy-monitor

# 6. cookies.json 单独处理（root-owned, 644；monitor 读但不写）
chmod 644 /etc/fliggy-monitor/cookies.json
chown root:monitor /etc/fliggy-monitor/cookies.json

echo "=== VPS side ready ==="
EOF
```

---

## 8.3 本机生成 SSH 密钥对

```bash
# 本机
ssh-keygen -t ed25519 -C "github-actions@fliggy-monitor" \
    -f ~/.ssh/id_fliggy-monitor -N ""

# 公钥 → VPS authorized_keys（已在 8.2 第 3 步放入）
cat ~/.ssh/id_fliggy-monitor.pub

# 私钥 → GitHub Secret（在 8.6 设置）
cat ~/.ssh/id_fliggy-monitor | base64 -w0
```

---

## 8.4 创建 GitHub 项目

1. 登录 GitHub → New repository
2. Owner: `666-XCJ`（或你的 org）
3. Repository name: `fliggy-monitor`
4. Visibility: **Private**
5. ☐ Add a README（不勾，我们自己推）
6. Create repository

URL 形如 `git@github.com:666-XCJ/fliggy-monitor.git`。

---

## 8.5 本机推送代码

```bash
cd /Users/argo/666-XCJ/fliggy-monitor

git init -b main
git add -A
git commit -m "init: phase 2-4 — web skeleton + monitor + GitHub Actions"
git remote add origin git@github.com:666-XCJ/fliggy-monitor.git
git push -u origin main
```

`.gitignore` 已写好（`cookies.json` / `.db` / 私钥 / `backups/` 都不会进仓）。

---

## 8.6 GitHub Secrets（在 GitHub UI 配）

**路径**：Repo → Settings → Secrets and variables → Actions → New repository secret

| Name | Value | 说明 |
|---|---|---|
| `VPS_HOST` | `107.172.144.102` | VPS 公网 IP |
| `VPS_USER` | `monitor` | 部署用户 |
| `VPS_PORT` | `22` | SSH 端口 |
| `VPS_SSH_KEY` | `<步骤 8.3 的私钥 base64>` | 用于 ssh / rsync |

---

## 8.7 `.github/workflows/deploy.yml`

完整内容见项目根 `.github/workflows/deploy.yml`。要点：

```yaml
on:
  push:
    branches: [main]
    tags: ['v*']
  workflow_dispatch:

jobs:
  lint:    # ruff + py_compile
  test:    # pytest
  build:   # tar + sha256 + 上传 artifact（保留 30d）
  deploy_production:
    needs: [build]
    if: github.event_name == 'push' && (github.ref == 'refs/heads/main' || startsWith(github.ref, 'refs/tags/v'))
    environment:
      name: production
      url: https://feizhu.19880913.xyz
    steps:
      - uses: actions/checkout@v4
      - run: apt-get install -y openssh-client rsync
      - run: echo "${{ secrets.VPS_SSH_KEY }}" > ~/.ssh/id_ed25519 && chmod 600 ~/.ssh/id_ed25519
      - run: ssh-keyscan -H "${{ secrets.VPS_HOST }}" > ~/.ssh/known_hosts
      - run: rsync ... (cookies.json / .db / .venv 排除)
      - run: ssh ... "sudo systemctl restart fliggy-web"
      - run: ssh ... "curl -fsS http://127.0.0.1:8080/healthz"
```

`deploy_production` 通过 `environment: production` 设置为 manual——合并到 main 后**不会**自动部署，需要去 Actions UI 点 "Run workflow"。

---

## 8.8 `requirements.txt`

```txt
fastapi>=0.115
uvicorn[standard]>=0.32
jinja2>=3.1
python-multipart>=0.0.18
httpx>=0.27
playwright>=1.48
```

---

## 8.9 部署流程图

```
[本机]                                          [GitHub]                    [VPS]
git push origin main  ──────────────────────►   Actions pipeline:
                                                       │
                                                       ├─ lint (auto)
                                                       ├─ test (auto)
                                                       ├─ build (auto, 产物 30d)
                                                       │
                                                       └─ deploy_production (manual)
                                                             │
                                                             ├─ ssh monitor@107.172.144.102
                                                             ├─ rsync --exclude cookies.json, .db
                                                             ├─ pip install -r requirements.txt
                                                             ├─ sudo systemctl restart fliggy-web
                                                             └─ curl /healthz
```

---

## 8.10 紧急回滚

GitHub UI → Actions → 找上一个 successful workflow → "Re-run all jobs"。

或 SSH 到 VPS：
```bash
ssh monitor@107.172.144.102
cd /opt/fliggy-monitor
git log --oneline -10
git checkout <last-good-commit> -- code web scripts
sudo systemctl restart fliggy-web
```

---

## 8.11 Cookie / 敏感数据处理

`.gitignore` 已排除 `cookies.json`。VPS 上 `/etc/fliggy-monitor/cookies.json` **永不被 deploy 覆盖**：
- rsync `--exclude='cookies.json'` 不会覆盖 VPS 上的 `/etc/fliggy-monitor/cookies.json`（路径不同）
- cookies 路径统一用 `/etc/fliggy-monitor/cookies.json`（config 表里写死）

**首次注入**（手工，从浏览器抓）：
```bash
ssh root@107.172.144.102
cat > /etc/fliggy-monitor/cookies.json << 'JSON'
{ ... }
chmod 644 /etc/fliggy-monitor/cookies.json
chown root:monitor /etc/fliggy-monitor/cookies.json
```

**自动续期**：`scripts/refresh_cookies.py` 通过 systemd timer 跑。

---

## 8.12 第一次部署 checklist

- [ ] VPS 上 8.2 步骤完成
- [ ] 本机 8.3 步骤完成（生成 ed25519 密钥对）
- [ ] GitHub UI 8.4 创建项目（Private）
- [ ] 本机 8.5 推送代码
- [ ] GitHub UI 8.6 配 Secrets（4 个）
- [ ] 第一次 push 后 Actions 自动跑 lint + test + build
- [ ] 手动点 deploy_production，看 Actions 日志
- [ ] SSH 到 VPS 看 `journalctl -u fliggy-web` 确认服务起来了
- [ ] 浏览器访问 https://feizhu.19880913.xyz/login
- [ ] 注入 cookies（8.11），跑 `scripts/refresh_cookies.py`