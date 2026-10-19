#!/usr/bin/env bash
# #123：验证沙箱到宿主机的访问只剩模型网关端口（打开出站代理时再加上代理端口）。
#
# 在沙箱网络里起一个和真实沙箱同样受限的临时容器，从里面连宿主机网桥地址上的端口：
#   - 网关端口：应当连得上（平台没在跑时是「拒绝」，只提示）；
#   - 其它端口（默认 22 80 443，可用 PROBE_PORTS 追加）：应当「超时」= 被防火墙丢弃。
#     「拒绝」也算失败：说明包到了宿主机的 TCP 协议栈，防火墙没拦住，换个端口就可能有服务。
#
# 出站代理（设了 NCS_EGRESS_PORT，NCS_EGRESS_PROXY=1 的沙箱主机）还会检查：
#   - 沙箱里没有 DNS，也不能绕过代理直连公网（PROBE_DIRECT，默认 1.1.1.1:443）；
#   - 经代理能取到一个公网 HTTPS 页面（PROBE_URL，默认 https://example.com/）；
#   - 代理拒绝（403）内网 / 元数据 / 本机地址和 80/443 以外的端口：
#     10.0.0.1:443、169.254.169.254:80、127.0.0.1:443、宿主机网桥地址:22、PROBE_HOST:22，
#     以及明文 GET http://169.254.169.254/latest/meta-data/。
# 退出码：0 通过，1 有项目没通过，2 用法错误。
set -euo pipefail

NET="${NCS_DOCKER_NETWORK:-ncs-sandboxes}"
IMAGE="${NCS_SANDBOX_IMAGE:-nanocode-sandbox:latest}"
PORT="${NCS_GATEWAY_PORT:-8701}"
EGRESS_PORT="${NCS_EGRESS_PORT:-}"
PROBE_PORTS="${PROBE_PORTS:-22 80 443}"
PROBE_URL="${PROBE_URL:-https://example.com/}"
PROBE_DIRECT="${PROBE_DIRECT:-1.1.1.1:443}"
DOCKER="${DOCKER_CMD:-docker}"

case "${EGRESS_PORT}" in *[!0-9]*) echo "NCS_EGRESS_PORT 必须是端口号：${EGRESS_PORT}" >&2; exit 2 ;; esac

GW="$("${DOCKER}" network inspect -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}' "${NET}")"
if [ -z "${GW}" ]; then
  echo "读不到沙箱网络 ${NET} 的网关地址（平台启动过一次才会建这个网络）" >&2; exit 2
fi
echo "沙箱网络 ${NET}，宿主机网桥地址 ${GW}，网关端口 ${PORT}，出站代理端口 ${EGRESS_PORT:-（未启用）}，探测端口 ${PROBE_PORTS}"

"${DOCKER}" run --rm --network "${NET}" --read-only --cap-drop ALL --security-opt no-new-privileges \
  --user 10001:10001 --entrypoint python "${IMAGE}" -c '
import socket, sys, urllib.error, urllib.parse, urllib.request
gw, gateway_port, ports = sys.argv[1], int(sys.argv[2]), [int(p) for p in sys.argv[3].split()]
egress_port = int(sys.argv[4]) if sys.argv[4] else 0
probe_url, direct = sys.argv[5], sys.argv[6]
def probe(port, host=None):
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect((host or gw, port)); return "open"
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
if egress_port:
    r = probe(egress_port)
    print(f"  {egress_port}/tcp（出站代理）：{r}", "OK" if r == "open" else "失败：连不上出站代理（NCS_EGRESS_PROXY=1？防火墙放行了？）")
    bad += r != "open"
for p in ports:
    if p in (gateway_port, egress_port):
        continue
    r = probe(p)
    ok = r == "timeout"
    print(f"  {p}/tcp：{r}", "OK（被丢弃）" if ok else "失败：沙箱能到宿主机这个端口")
    bad += not ok
if egress_port:
    host = urllib.parse.urlsplit(probe_url).hostname
    try:
        socket.getaddrinfo(host, 443)
        print(f"  DNS {host}：能解析", "失败：沙箱里不该有 DNS"); bad += 1
    except OSError:
        print(f"  DNS {host}：解析不了 OK")
    dhost, dport = direct.rsplit(":", 1)
    r = probe(int(dport), dhost)
    print(f"  直连 {direct}：{r}", "失败：沙箱能绕过代理直连公网" if r == "open" else "OK（连不上）")
    bad += r == "open"
    proxy = f"http://{gw}:{egress_port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    try:
        with opener.open(probe_url, timeout=20) as resp:
            print(f"  经代理取 {probe_url}：HTTP {resp.status} OK")
    except urllib.error.HTTPError as e:
        print(f"  经代理取 {probe_url}：HTTP {e.code} OK（连上了）")
    except OSError as e:
        print(f"  经代理取 {probe_url}：{e}", "失败：代理取不到公网 HTTPS"); bad += 1
    def ask(request):
        s = socket.create_connection((gw, egress_port), timeout=15)
        try:
            s.sendall(request.encode())
            line = s.recv(4096).split(b"\r\n", 1)[0].decode("latin-1")
        except OSError as e:
            line = type(e).__name__
        finally:
            s.close()
        return line
    for target in ("10.0.0.1:443", "169.254.169.254:80", "127.0.0.1:443", f"{gw}:22", f"{host}:22"):
        line = ask(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n")
        ok = line.startswith("HTTP/1.1 403")
        print(f"  CONNECT {target}：{line}", "OK（拒绝）" if ok else "失败：代理没拒绝")
        bad += not ok
    line = ask("GET http://169.254.169.254/latest/meta-data/ HTTP/1.1\r\nHost: 169.254.169.254\r\n\r\n")
    ok = line.startswith("HTTP/1.1 403")
    print(f"  GET http://169.254.169.254/：{line}", "OK（拒绝）" if ok else "失败：代理没拒绝元数据地址")
    bad += not ok
print("通过" if not bad else f"{bad} 项没通过：检查 deploy/firewall/README.md")
sys.exit(1 if bad else 0)
' "${GW}" "${PORT}" "${PROBE_PORTS}" "${EGRESS_PORT}" "${PROBE_URL}" "${PROBE_DIRECT}"
