# 沙箱到宿主机的防火墙（#123）

Docker 沙箱放在一个 internal 网络里：出不了外网，容器之间也不通。但宿主机在这个网桥上有一个地址（模型网关就监听在那里），**internal 网络不限制容器访问这个地址上的其它端口**。宿主机上任何监听 `0.0.0.0` / `::` 的服务（sshd、nginx、别的管理端口）沙箱都连得上。

AlmaLinux / RHEL 上更明显：Docker 会把自己建的网桥放进 firewalld 的 `docker` zone，这个 zone 是 `target: ACCEPT`，等于对沙箱全开。

所以，**跑 Docker 沙箱的机器必须配这一层防火墙**：沙箱进来的连接只放行到模型网关端口，其它全部丢弃。

## 平台这边固定了什么

| 变量 | 默认 | 作用 |
|---|---|---|
| `NCS_DOCKER_BRIDGE` | `ncs-sbx0` | 沙箱网桥的网卡名（`com.docker.network.bridge.name`） |
| `NCS_DOCKER_SUBNET` | `172.30.250.0/24` | 沙箱网段，宿主机网桥地址是其中第一个（`.1`） |
| `NCS_GATEWAY_PORT` | `8701` | 模型网关端口，沙箱唯一允许连的宿主机端口 |

平台第一次启动时按这两个值创建沙箱网络。网络已经存在但网桥名或网段对不上时，**平台拒绝启动**：防火墙规则按它们写，对不上就等于没配。这时删掉旧网络（`docker network rm ncs-sandboxes`，先停掉所有沙箱），让平台重建即可。网段如果和机器上已有的网络冲突，换一个网段，防火墙脚本用同一个值。

## 有 firewalld（AlmaLinux / RHEL / Rocky 默认）

```bash
sudo NCS_DOCKER_SUBNET=172.30.250.0/24 NCS_GATEWAY_PORT=8701 ./ncs-sandbox-firewalld.sh
```

它新建一个 `ncs-sandbox` zone：`target: DROP`，只开网关端口，**按来源网段**把沙箱网段绑进去。可以重复执行。

为什么按来源网段而不是网卡：Docker 会把网桥放进 `docker` zone，而且 Docker 重启时会再放回去，跟它抢网卡不可靠。firewalld 先按来源网段匹配 zone，再按网卡匹配，所以来源网段的 zone 总是先生效，`docker` zone 的 ACCEPT 碰不到沙箱流量。已建立连接的回包（控制面主动连沙箱的 8765 端口）在 firewalld 进入 zone 之前就放行了，不受影响。

### 默认 zone / eth0 的注意事项

- `firewall-cmd --list-all` 看到 `public` zone 的 `interfaces:` 是空的很常见：没有显式绑定的网卡（比如 eth0）走**默认 zone**（`firewall-cmd --get-default-zone`）。这不影响沙箱规则（来源网段优先），但要确认默认 zone 是你以为的那个，不要是 `trusted`。想显式绑定：`firewall-cmd --permanent --zone=public --change-interface=eth0 && firewall-cmd --reload`。
- **不要**把沙箱网段或 `ncs-sbx0` 加进 `trusted` 或别的 zone。一个网段只能属于一个 zone，脚本发现它在别的 zone 里会先移出来。
- 如果机器上开了 firewalld 的 zone 优先级（firewalld 2.x 的 `ingress-priority`），别把别的 zone 排到 `ncs-sandbox` 前面。

## 没有 firewalld（Debian / Ubuntu 等）

```bash
sudo NCS_DOCKER_BRIDGE=ncs-sbx0 NCS_DOCKER_SUBNET=172.30.250.0/24 NCS_GATEWAY_PORT=8701 ./ncs-sandbox-nftables.sh
```

它用一张独立的 nftables 表（`inet ncs_sandbox`），按网卡名和来源网段丢弃，只放行网关端口和已建立连接。规则写在 `/etc/nanocode/ncs-sandbox.nft`；要开机生效，在发行版的 nftables 主配置里 `include` 这个文件，并 `systemctl enable nftables`。

## 验证

平台启动过一次（沙箱网络已经建好）、沙箱镜像在本机时：

```bash
./check-sandbox-isolation.sh          # PROBE_PORTS="22 80 443 5432" 可追加要探测的端口
```

它在沙箱网络里起一个和真实沙箱同样受限的临时容器，去连宿主机网桥地址上的端口。网关端口应该连得上，其它端口应该**超时**（被丢弃）。「拒绝」也算没拦住：包到了宿主机协议栈，换个端口就可能有服务。

## 沙箱放在另一台机器上时

跑沙箱的那台机器上同样要配，网关端口换成那台机器上沙箱实际连的端口。
