#!/usr/bin/env bash
# #123：没有 firewalld 的机器用这个。效果同 ncs-sandbox-firewalld.sh：
# 沙箱网桥 / 沙箱网段进来的连接，只放行到本机模型网关端口，其余丢弃。
#
# 用一张独立的 nftables 表（inet ncs_sandbox），不碰 Docker 和发行版自己的规则：
# nftables 里任何一个 base chain 丢弃就是最终结果，所以这张表的 drop 不会被别处的 accept 盖掉。
# 已建立的连接（控制面主动连沙箱的回包）放行。
#
# 用法（root）：
#   NCS_DOCKER_BRIDGE=ncs-sbx0 NCS_DOCKER_SUBNET=172.30.250.0/24 NCS_GATEWAY_PORT=8701 ./ncs-sandbox-nftables.sh
# 规则写到 $NFT_FILE（默认 /etc/nanocode/ncs-sandbox.nft）并立即加载；要开机生效，
# 在发行版的 nftables 主配置里 include 这个文件（RHEL 系：/etc/sysconfig/nftables.conf；
# Debian 系：/etc/nftables.conf），并 systemctl enable nftables。
set -euo pipefail

BRIDGE="${NCS_DOCKER_BRIDGE:-ncs-sbx0}"
SUBNET="${NCS_DOCKER_SUBNET:-172.30.250.0/24}"
PORT="${NCS_GATEWAY_PORT:-8701}"
NFT="${NFT_CMD:-nft}"
NFT_FILE="${NFT_FILE:-/etc/nanocode/ncs-sandbox.nft}"

case "$PORT" in ''|*[!0-9]*) echo "NCS_GATEWAY_PORT 必须是端口号：$PORT" >&2; exit 2 ;; esac
if ! [[ "$SUBNET" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]]; then
  echo "NCS_DOCKER_SUBNET 必须是 IPv4 网段：$SUBNET" >&2; exit 2
fi
if ! [[ "$BRIDGE" =~ ^[A-Za-z0-9_.-]{1,15}$ ]]; then
  echo "NCS_DOCKER_BRIDGE 不是合法网卡名：$BRIDGE" >&2; exit 2
fi

mkdir -p "$(dirname "$NFT_FILE")"
cat > "$NFT_FILE" <<NFT
# 由 deploy/firewall/ncs-sandbox-nftables.sh 生成（#123）
table inet ncs_sandbox
delete table inet ncs_sandbox
table inet ncs_sandbox {
  chain input {
    type filter hook input priority -5; policy accept;
    iifname "$BRIDGE" ct state established,related accept
    iifname "$BRIDGE" tcp dport $PORT accept
    iifname "$BRIDGE" counter drop
    ip saddr $SUBNET ct state established,related accept
    ip saddr $SUBNET tcp dport $PORT accept
    ip saddr $SUBNET counter drop
  }
}
NFT
"$NFT" -f "$NFT_FILE"
echo "完成：$BRIDGE / $SUBNET 进来的连接只放行到本机 $PORT/tcp（规则文件 $NFT_FILE）。"
