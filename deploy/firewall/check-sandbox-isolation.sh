#!/usr/bin/env bash
# #123：验证沙箱到宿主机的访问只剩模型网关端口。
#
# 在沙箱网络里起一个和真实沙箱同样受限的临时容器，从里面连宿主机网桥地址上的端口：
#   - 网关端口：应当连得上（平台没在跑时是「拒绝」，只提示）；
#   - 其它端口（默认 22 80 443，可用 PROBE_PORTS 追加）：应当「超时」= 被防火墙丢弃。
#     「拒绝」也算失败：说明包到了宿主机的 TCP 协议栈，防火墙没拦住，换个端口就可能有服务。
# 退出码：0 通过，1 有端口没拦住。
set -euo pipefail

NET="${NCS_DOCKER_NETWORK:-ncs-sandboxes}"
IMAGE="${NCS_SANDBOX_IMAGE:-nanocode-sandbox:latest}"
PORT="${NCS_GATEWAY_PORT:-8701}"
PROBE_PORTS="${PROBE_PORTS:-22 80 443}"
DOCKER="${DOCKER_CMD:-docker}"

GW="$("${DOCKER}" network inspect -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}' "${NET}")"
if [ -z "${GW}" ]; then
  echo "读不到沙箱网络 ${NET} 的网关地址（平台启动过一次才会建这个网络）" >&2; exit 2
fi
echo "沙箱网络 ${NET}，宿主机网桥地址 ${GW}，网关端口 ${PORT}，探测端口 ${PROBE_PORTS}"

"${DOCKER}" run --rm --network "${NET}" --read-only --cap-drop ALL --security-opt no-new-privileges \
  --user 10001:10001 --entrypoint python "${IMAGE}" -c '
import socket, sys
gw, gateway_port, ports = sys.argv[1], int(sys.argv[2]), [int(p) for p in sys.argv[3].split()]
def probe(port):
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect((gw, port)); return "open"
    except socket.timeout:
        return "timeout"
    except ConnectionRefusedError:
        return "refused"
    except OSError as e:
        return "error:" + type(e).__name__
    finally:
        s.close()
bad = 0
r = probe(gateway_port)
print(f"  {gateway_port}/tcp（网关）：{r}", "OK" if r == "open" else ("提示：网关没在监听（平台没启动？）" if r == "refused" else "失败：网关被拦了"))
bad += r not in ("open", "refused")
for p in ports:
    if p == gateway_port:
        continue
    r = probe(p)
    ok = r == "timeout"
    print(f"  {p}/tcp：{r}", "OK（被丢弃）" if ok else "失败：沙箱能到宿主机这个端口")
    bad += not ok
print("通过" if not bad else f"{bad} 项没通过：检查 deploy/firewall/README.md")
sys.exit(1 if bad else 0)
' "${GW}" "${PORT}" "${PROBE_PORTS}"
