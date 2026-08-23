# 08 · GitHub Actions 部署（私有仓 + 自动 Deploy）

> 把代码托管在 GitHub 私有仓，`main` 分支每次推送 / 手动 `workflow_dispatch` 触发 lint + test + build + deploy_production → 自动 SSH 到 107.172.144.102 → rsync 拉最新 → 重启服务。

---

## 8.1 前置假设

| 项 | 假定 | 备注 |
|---|---|---|
| GitHub 仓 | 私有仓 `youttiao/fliggy-monitor` | 用户在 GitHub UI 创建 |
| Runner | GitHub-hosted（ubuntu-latest） | 默认免费额度足够 |
| SSH key | ed25519 部署密钥（**VPS 上生成**） | 见 8.3 解释为什么不放 Secret |
| VPS 用户 | `root` | 见 [05 §5.3](05-deployment-vps.md) — 已放弃 `monitor` 专用用户 |
| 部署触发 | push 到 `main` 或 `workflow_dispatch` | 都跑同一份 deploy |

仓路径：`github.com/youttiao/fliggy-monitor`（与本地目录 `/Users/argo/666-XCJ/fliggy-monitor` 不一致，deploy 不依赖目录名）。

---

## 8.2 一次性准备（VPS 端）

```bash
ssh root@107.172.144.102 << 'EOF'
set -e

# 1. /opt/fliggy-monitor 必须存在（05 部署时已建好）
ls -ld /opt/fliggy-monitor

# 2. cookies.json 注入（手动，见 8.11）
mkdir -p /etc/fliggy-monitor
chmod 755 /etc/fliggy-monitor

# 3. logs 目录（systemd 的 StandardOutput=append: 需要存在）
mkdir -p /opt/fliggy-monitor/logs

echo "=== VPS side ready ==="
EOF
```

---

## 8.3 SSH 密钥设计：GitHub Deploy Key（不放 Secret）

**为什么不用 GitHub Secret 存私钥？**

| 方案 | 优点 | 缺点 |
|---|---|---|
| GitHub Secret `VPS_SSH_KEY` | 私钥不进仓 | Secret 会出现在环境变量 / 日志里；轮换需要 UI 操作；泄露面广 |
| **GitHub Deploy Key**（read-only） | 私钥仅在 VPS 磁盘，权限绑死到单个仓 | 需要在 VPS 上生成密钥对 |

本项目用 **Deploy Key**：
1. VPS 上生成专用 ed25519 密钥对（只给 fliggy-monitor 用）
2. 公钥加到 GitHub 仓的 Settings → Deploy keys（**勾 "Allow read access"**）
3. 私钥留在 VPS `/root/.ssh/fliggy_deploy_key`
4. CI runner 端通过 GitHub Secret `VPS_SSH_KEY` 拿到**同一份**私钥内容写入 `~/.ssh/id_ed25519`

> 这里的 `VPS_SSH_KEY` Secret 实际上只是把 VPS 上的私钥**搬运**到 runner，跟传统 Secret 用法不一样——本质还是 VPS 自管的部署密钥。

**生成步骤**（在 VPS 上执行）：
```bash
ssh root@107.172.144.102
ssh-keygen -t ed25519 -C "github-actions@fliggy-monitor" \
    -f /root/.ssh/fliggy_deploy_key -N ""
chmod 600 /root/.ssh/fliggy_deploy_key

# 公钥 → GitHub repo Settings → Deploy keys → Add deploy key
cat /root/.ssh/fliggy_deploy_key.pub

# 把公钥也加入 authorized_keys（runner 用同一对密钥 SSH 回 VPS）
cat /root/.ssh/fliggy_deploy_key.pub >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

# 自测
ssh -i /root/.ssh/fliggy_deploy_key -o IdentitiesOnly=yes \
    root@107.172.144.102 'echo deploy-key works'
```

**私钥 → GitHub Secret**（直接粘贴私钥原文，不做 base64）：
```bash
# GitHub Secret 字符串 = /root/.ssh/fliggy_deploy_key 的内容（含 BEGIN/END 标记、换行）
cat /root/.ssh/fliggy_deploy_key
```

---

## 8.4 创建 GitHub 项目

1. 登录 GitHub → New repository
2. Owner: `youttiao`
3. Repository name: `fliggy-monitor`
4. Visibility: **Private**
5. ☐ Add a README（不勾，我们自己推）
6. Create repository

URL：`git@github.com:youttiao/fliggy-monitor.git`

---

## 8.5 本机推送代码

```bash
cd /Users/argo/666-XCJ/fliggy-monitor

git remote add origin git@github.com:youttiao/fliggy-monitor.git  # 若未配
git push -u origin main
```

`.gitignore` 已写好（`cookies.json` / `.db` / 私钥 / `backups/` 都不会进仓）。

---

## 8.6 GitHub Secrets（在 GitHub UI 配）

**路径**：Repo → Settings → Secrets and variables → Actions → New repository secret

| Name | Value | 说明 |
|---|---|---|
| `VPS_HOST` | `107.172.144.102` | VPS 公网 IP |
| `VPS_USER` | `root` | 部署用户 |
| `VPS_PORT` | `22` | SSH 端口 |
| `VPS_SSH_KEY` | `<步骤 8.3 的私钥 base64>` | CI runner 用来 SSH 回 VPS |

**额外**：Repo → Settings → Deploy keys → Add deploy key
- Title: `vps-107.172.144.102`
- Key: `<步骤 8.3 的公钥>`
- ✅ Allow read-only access

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
    if: |
      (github.event_name == 'push' &&
        (github.ref == 'refs/heads/main' || startsWith(github.ref, 'refs/tags/v'))) ||
      github.event_name == 'workflow_dispatch'
    environment:
      name: production
      url: https://feizhu.19880913.xyz
    steps:
      - uses: actions/checkout@v4
      - run: apt-get install -y openssh-client rsync
      - run: echo "${{ secrets.VPS_SSH_KEY }}" > ~/.ssh/id_ed25519 && chmod 600 ~/.ssh/id_ed25519
      - run: ssh-keyscan -H "${{ secrets.VPS_HOST }}" > ~/.ssh/known_hosts
      - run: rsync -avz --delete ... ./ root@$HOST:/opt/fliggy-monitor/   # 排除 cookies.json, .db, .venv
      - run: ssh root@$HOST ".venv/bin/pip install -r requirements.txt && systemctl restart fliggy-web"
      - run: ssh root@$HOST "curl -fsS http://127.0.0.1:8080/healthz"
```

`deploy_production` 通过 `environment: production` 暴露 manual gate——但**当前配置下 push 会自动部署**（因为 CI 是单人开发环境）。若需要 PR 合并前审核，把 `environment` 改用 `protected branches` + required reviewers。

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
                                                       └─ deploy_production (auto on push)
                                                             │
                                                             ├─ ssh root@107.172.144.102
                                                             ├─ rsync --delete --exclude cookies.json, .db, .venv
                                                             ├─ pip install -r requirements.txt
                                                             ├─ systemctl restart fliggy-web
                                                             └─ curl /healthz
```

---

## 8.10 紧急回滚

GitHub UI → Actions → 找上一个 successful workflow → "Re-run all jobs"。

或 SSH 到 VPS：
```bash
ssh root@107.172.144.102
cd /opt/fliggy-monitor
git log --oneline -10
git checkout <last-good-commit> -- code web scripts
.venv/bin/pip install -r requirements.txt
systemctl restart fliggy-web
```

---

## 8.11 Cookie / 敏感数据处理

`.gitignore` 已排除 `cookies.json`。VPS 上 `/etc/fliggy-monitor/cookies.json` **永不被 deploy 覆盖**：
- rsync `--exclude='cookies.json'` 只在 `/opt/fliggy-monitor/` 范围内生效
- cookies 路径统一用 `/etc/fliggy-monitor/cookies.json`（与项目目录分离，rsync 不会触碰）

**首次注入**（手工，从浏览器抓）：
```bash
ssh root@107.172.144.102
cat > /etc/fliggy-monitor/cookies.json << 'JSON'
{ ... }
chmod 644 /etc/fliggy-monitor/cookies.json
```

**自动续期**：`scripts/refresh_cookies.py` 通过 systemd timer 跑（mtop `_m_h5_tk` ~2h 滑动窗口，90 分钟一刷）。

---

## 8.12 第一次部署 checklist（实际跑通过版本）

- [x] VPS 上 8.2 步骤完成（`/opt/fliggy-monitor`、`/etc/fliggy-monitor`、`logs/`）
- [x] VPS 上 8.3 生成 ed25519 部署密钥对
- [x] GitHub UI 8.4 创建项目（Private, `youttiao/fliggy-monitor`）
- [x] GitHub UI 8.6 配 Secrets（4 个）+ Deploy key（read-only）
- [x] 本机 8.5 推送代码
- [x] push 后 Actions 自动跑 lint + test + build
- [x] deploy_production 跑通（rsync + 远程 install + restart + /healthz 200）
- [x] HTTPS `https://feizhu.19880913.xyz/healthz` 返回 200 valid JSON
- [x] HTTPS `https://feizhu.19880913.xyz/login` 返回 200 渲染正常
- [ ] 注入 cookies 到 `/etc/fliggy-monitor/cookies.json`（8.11）
- [ ] 跑 `scripts/refresh_cookies.py` 验证续期
- [ ] 手动触发一轮 `fliggy-monitor.service` 看能否抓到 sellers
- [ ] 配置 webhook URL（settings 页），跑一次告警链路