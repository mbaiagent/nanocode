# 持续部署（CD）：GitHub Actions 经 SSH 部署

适用：分布式形态（平台机 + 沙箱机，见 [distributed-deploy.md](distributed-deploy.md)）。单机 compose / 本地模式不需要这个。

## 流程

1. PR 合并到 `main` → `CI` 在 main 上跑完且**成功**；
2. `.github/workflows/deploy.yml` 被 `workflow_run` 触发，进入 `production` 环境（只允许 `main`）；
3. 先 SSH 到**沙箱机**，再 SSH 到**平台机**，每台只发一句 `deploy <40 位 sha>`；
4. 服务器上的 `nanocode-deploy`（root）：校验 sha → 用 GitHub API 确认该提交在 `main` 上 → 从 `codeload.github.com` 下载该提交的 tarball → 解到新版本目录 → 拷贝 `.env` / override 等本机文件 → 原子切换软链接 → 重建 → 健康检查 → 失败自动切回上一个版本并重建；
5. 保留最近 3 个版本目录。

也可以在 Actions 页面手动触发（`workflow_dispatch`，可填 sha）。想改成"打 `v*` tag 才部署"：在 deploy.yml 里删掉 `workflow_run` 段、打开 `push.tags` 段。

## 安全模型

| 层 | 措施 |
|---|---|
| GitHub | 环境 `production` 只允许 `main` 部署；SSH 私钥、主机地址、known_hosts 都是**环境密钥**，PR / fork 的 workflow 拿不到；workflow 权限 `contents: read`；不用任何第三方 action（不 checkout 代码，没有要 pin 的 action） |
| SSH | 每台机器一把独立 ed25519 key；`StrictHostKeyChecking=yes` + 密钥里的 known_hosts（指纹和服务器 `/etc/ssh` 下的主机密钥核对过）；`BatchMode` |
| 服务器账号 | 专用用户 `deploy`，无密码（锁定）；`authorized_keys` 由 root 所有，`restrict,command="/usr/local/sbin/nanocode-deploy-ssh"`：无 pty、无转发，不管客户端发什么命令都只会跑强制命令 |
| 强制命令 | `nanocode-deploy-ssh` 只接受 `deploy <40 位十六进制>`、`status`、`rollback`，其余拒绝 |
| sudo | `/etc/sudoers.d/nanocode-deploy` 只允许以 root 跑 `/usr/local/sbin/nanocode-deploy` 的这三种参数；脚本 root 所有、0755，deploy 用户改不了 |
| 部署脚本 | 再次校验 sha；只部署 `main` 上的提交；只从 GitHub https 下载（公开仓库，不需要凭据）；配置在 root 所有的 `/etc/nanocode-deploy.conf`；`flock` 防并发 |

泄露 deploy 私钥的最坏情况：攻击者能把服务器重新部署成 **main 上已有的某个提交**（包括旧提交）或回滚——不能执行任意命令、不能拿 shell、不能部署 main 以外的代码。处理：删掉服务器上对应的 `authorized_keys` 行，换新 key。

没加 `from=` 限制：GitHub 托管 runner 的出口 IP 范围很大且经常变。需要的话可以改用自托管 runner 再加 `from=`。

## 配置步骤（每台机器）

```bash
# 1. 用户（无密码、锁定）
useradd --create-home --shell /bin/bash deploy && passwd -l deploy
# 2. 脚本（root 所有，0755）
install -o root -g root -m 0755 deploy/cd/nanocode-deploy deploy/cd/nanocode-deploy-ssh /usr/local/sbin/
# 3. sudoers（先检查再放）
visudo -cf deploy/cd/nanocode-deploy.sudoers && install -o root -g root -m 0440 deploy/cd/nanocode-deploy.sudoers /etc/sudoers.d/nanocode-deploy
# 4. 配置：参考 deploy/cd/nanocode-deploy.conf.example，写 /etc/nanocode-deploy.conf（root:root 0644）
# 5. 公钥（在自己机器上 ssh-keygen -t ed25519 -N '' -f deploy_<host>，私钥只放进 GitHub 密钥）
install -d -o root -g root -m 0755 /home/deploy/.ssh
echo 'restrict,command="/usr/local/sbin/nanocode-deploy-ssh" ssh-ed25519 AAAA... nanocode-cd' > /home/deploy/.ssh/authorized_keys
chown root:root /home/deploy/.ssh/authorized_keys && chmod 0644 /home/deploy/.ssh/authorized_keys
```

第一次运行时，如果 `LIVE`（例如 `/opt/nanocode`）还是普通目录，脚本会把它挪进 `RELEASES` 并换成软链接，`.env` 等文件跟着走。

GitHub 侧：

```bash
# 环境：只允许 main
gh api -X PUT repos/<owner>/<repo>/environments/production \
  -F 'deployment_branch_policy[protected_branches]=false' -F 'deployment_branch_policy[custom_branch_policies]=true'
gh api -X POST repos/<owner>/<repo>/environments/production/deployment-branch-policies -f name=main -f type=branch
# 密钥（从文件管道输入，不要回显）
gh secret set DEPLOY_SSH_KEY_PLATFORM --env production < deploy_platform
gh secret set DEPLOY_SSH_KEY_SANDBOX  --env production < deploy_sandbox
gh secret set DEPLOY_HOST_PLATFORM    --env production --body '<平台机地址>'
gh secret set DEPLOY_HOST_SANDBOX     --env production --body '<沙箱机地址>'
ssh-keyscan -t ed25519 <平台机> <沙箱机> > kh   # 和服务器 /etc/ssh/ssh_host_ed25519_key.pub 核对指纹
gh secret set DEPLOY_KNOWN_HOSTS      --env production < kh
shred -u deploy_platform deploy_sandbox
```

## 回滚

- **自动**：新版本重建或健康检查失败时，脚本自动切回上一个版本并重建，Actions 这次 run 标红。
- **手动（从有 deploy key 的地方）**：`ssh deploy@<host> rollback`，切到上一个保留的版本目录并重建。
- **手动（root）**：`nanocode-deploy status` 看保留的版本；`nanocode-deploy rollback`；或者直接 `nanocode-deploy deploy <更早的 main sha>`。
- 两台都要回时，先回平台机，再回沙箱机。

健康检查：平台机 `HEALTH_URL` 必须返回 `ok`、`launcher: remote`、`isolated: true`（配了 `AGENT_HEALTH_URL` 还会探测沙箱代理）；沙箱机代理 `/healthz` 为 ok 且 `deploy/firewall/check-sandbox-isolation.sh` 通过。

## 停用 CD

在仓库 Settings → Environments → production 加 required reviewers（变成"人工批准后部署"），或者直接禁用 Deploy workflow；服务器上删掉 `/home/deploy/.ssh/authorized_keys` 即彻底断开。
