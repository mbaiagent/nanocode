#!/usr/bin/env bash
# #123：Docker 沙箱只能连宿主机上的模型网关端口，其它一律丢弃（sshd、nginx、管理端口……）。
#
# 做法：新建一个 firewalld zone（默认 ncs-sandbox），target=DROP，只开网关端口；
# 按「来源网段」把沙箱网段（NCS_DOCKER_SUBNET）绑进去。
#
# 为什么按来源网段、不按网卡：Docker 会把自己建的网桥（包括沙箱网桥 ncs-sbx0）放进
# firewalld 的 docker zone，那个 zone 是 target=ACCEPT，而且 Docker 重启时会再放回去。
# firewalld 先按来源网段分 zone、再按网卡分，所以来源网段的规则总是先生效，不用跟 Docker 抢网卡。
#
# 出站代理（NCS_EGRESS_PROXY=1，只在沙箱主机上）：再设 NCS_EGRESS_PORT=3128，这个端口也放行，
# 沙箱就只能连「网关中继」和「出站代理」两个端口；不设 = 只放行网关端口（和以前一样）。
#
# 用法（root，在跑 Docker 沙箱的那台机器上）：
#   NCS_DOCKER_SUBNET=172.30.250.0/24 NCS_GATEWAY_PORT=8701 [NCS_EGRESS_PORT=3128] ./ncs-sandbox-firewalld.sh
# 可以重复执行（幂等）。改完用 check-sandbox-isolation.sh 验证。
set -euo pipefail

SUBNET="${NCS_DOCKER_SUBNET:-172.30.250.0/24}"
PORT="${NCS_GATEWAY_PORT:-8701}"
EGRESS_PORT="${NCS_EGRESS_PORT:-}"
ZONE="${NCS_FIREWALL_ZONE:-ncs-sandbox}"
FW="${FIREWALL_CMD:-firewall-cmd}"

case "${PORT}" in ''|*[!0-9]*) echo "NCS_GATEWAY_PORT 必须是端口号：${PORT}" >&2; exit 2 ;; esac
case "${EGRESS_PORT}" in *[!0-9]*) echo "NCS_EGRESS_PORT 必须是端口号：${EGRESS_PORT}" >&2; exit 2 ;; esac
if [ -n "${EGRESS_PORT}" ] && [ "${EGRESS_PORT}" = "${PORT}" ]; then
  echo "NCS_EGRESS_PORT 不能和 NCS_GATEWAY_PORT 相同：${PORT}" >&2; exit 2
fi
if ! [[ "${SUBNET}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]]; then
  echo "NCS_DOCKER_SUBNET 必须是 IPv4 网段（例如 172.30.250.0/24）：${SUBNET}" >&2; exit 2
fi
if ! [[ "${ZONE}" =~ ^[A-Za-z0-9_-]{1,17}$ ]]; then
  echo "NCS_FIREWALL_ZONE 只能是字母、数字、_ -（最长 17）：${ZONE}" >&2; exit 2
fi

if ! "${FW}" --state >/dev/null 2>&1; then
  echo "firewalld 没在运行。没有 firewalld 的机器改用 ncs-sandbox-nftables.sh" >&2
  exit 1
fi

if ! "${FW}" --permanent --get-zones | tr ' ' '\n' | grep -qx "${ZONE}"; then
  "${FW}" --permanent --new-zone="${ZONE}" >/dev/null
fi

# 一个网段只能属于一个 zone：在别的 zone（比如有人加进了 trusted）里就先拿出来
other="$("${FW}" --permanent --get-zone-of-source="${SUBNET}" 2>/dev/null || true)"
if [ -n "${other}" ] && [ "${other}" != "${ZONE}" ]; then
  echo "网段 ${SUBNET} 原来在 zone ${other} 里，移到 ${ZONE}"
  "${FW}" --permanent --zone="${other}" --remove-source="${SUBNET}" >/dev/null
fi

"${FW}" --permanent --zone="${ZONE}" --set-target=DROP >/dev/null
# 专用 zone：清掉以前留下的服务 / 端口 / 其它来源，只留网关端口（和出站代理端口）
for s in $("${FW}" --permanent --zone="${ZONE}" --list-services); do
  "${FW}" --permanent --zone="${ZONE}" --remove-service="${s}" >/dev/null
done
for p in $("${FW}" --permanent --zone="${ZONE}" --list-ports); do
  if [ "${p}" != "${PORT}/tcp" ] && { [ -z "${EGRESS_PORT}" ] || [ "${p}" != "${EGRESS_PORT}/tcp" ]; }; then
    "${FW}" --permanent --zone="${ZONE}" --remove-port="${p}" >/dev/null
  fi
done
# rich rule 可能放行任意端口：专用 zone 里一条都不留（一行一条，里面有空格，按行读）
"${FW}" --permanent --zone="${ZONE}" --list-rich-rules | while IFS= read -r rule; do
  if [ -n "${rule}" ]; then
    "${FW}" --permanent --zone="${ZONE}" --remove-rich-rule="${rule}" >/dev/null
  fi
done
for s in $("${FW}" --permanent --zone="${ZONE}" --list-sources); do
  [ "${s}" = "${SUBNET}" ] || "${FW}" --permanent --zone="${ZONE}" --remove-source="${s}" >/dev/null
done
"${FW}" --permanent --zone="${ZONE}" --query-source="${SUBNET}" >/dev/null 2>&1 \
  || "${FW}" --permanent --zone="${ZONE}" --add-source="${SUBNET}" >/dev/null
"${FW}" --permanent --zone="${ZONE}" --query-port="${PORT}/tcp" >/dev/null 2>&1 \
  || "${FW}" --permanent --zone="${ZONE}" --add-port="${PORT}/tcp" >/dev/null
if [ -n "${EGRESS_PORT}" ]; then
  "${FW}" --permanent --zone="${ZONE}" --query-port="${EGRESS_PORT}/tcp" >/dev/null 2>&1 \
    || "${FW}" --permanent --zone="${ZONE}" --add-port="${EGRESS_PORT}/tcp" >/dev/null
fi
"${FW}" --reload >/dev/null

echo "完成：来自 ${SUBNET} 的连接只放行到本机 ${PORT}/tcp${EGRESS_PORT:+ 和 ${EGRESS_PORT}/tcp（出站代理）}，其余丢弃（zone ${ZONE}）。"
"${FW}" --zone="${ZONE}" --list-all
