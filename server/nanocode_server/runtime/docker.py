"""Docker：每个会话一个容器。单机上比本机子进程安全得多，适合一台服务器给一个团队用。

每个容器：非 root、只读根文件系统、去掉全部 Linux capabilities、禁止提权、限制 CPU / 内存 / 进程数；
工作区和 NANOCODE_HOME 在宿主机的 <数据目录>/sandboxes/<会话>/data 下（挂到 /data），
平台策略只读挂到 /etc/nanocode/。可以用 gVisor（NCS_DOCKER_RUNTIME=runsc）进一步隔离。

网络：沙箱放在一个专用的内部网络（internal bridge）里——容器之间不通，也出不了外网，
只能访问宿主机在这个网络上的地址，模型网关就监听在那里。internal 网络不限制这个地址上的其它端口，
所以宿主机还要配防火墙只放行网关端口（deploy/firewall/，#123）；网桥名和网段固定下来供防火墙引用。

通过 Docker Engine API（/var/run/docker.sock）操作，只用标准库。
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import time
from urllib.parse import quote

from .base import SANDBOX_PORT, Launcher, LaunchError, SandboxSpec, wait_ready

DOCKER_SOCKET = "/var/run/docker.sock"
UID = 10001
ICC_OPTION = "com.docker.network.bridge.enable_icc"
BRIDGE_NAME_OPTION = "com.docker.network.bridge.name"


def sandbox_ids() -> tuple[int, int]:
    """沙箱容器里以哪个 uid:gid 运行。

    控制面是 root：固定用镜像里的 10001，启动前把工作区 chown 给它（容器保持非 root）；
    控制面是普通用户：直接用控制面的 uid:gid——工作区本来就是控制面这个用户建的，
    而 chown 需要 root 权限、非 root 下只会失败。uid 不一致时容器连 /data 都写不了，
    nanocode 写不了会话和配置、Agent 写不了文件，wait_ready 超时后变成一个难查的 502（#58）。
    """
    if not hasattr(os, "geteuid"):
        # Windows 上的控制面（Docker Desktop）没有 POSIX uid：容器里用镜像自带的 10001
        return UID, UID
    if os.geteuid() == 0:
        return UID, UID
    return os.geteuid(), os.getegid()


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 60):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class DockerClient:
    def __init__(self, socket_path: str = DOCKER_SOCKET):
        self.socket_path = socket_path

    def request(self, method: str, path: str, body: dict | None = None, timeout: float = 60) -> tuple[int, dict]:
        conn = _UnixConnection(self.socket_path, timeout)
        try:
            headers = {"content-type": "application/json"} if body is not None else {}
            conn.request(method, "/v1.41" + path, body=json.dumps(body) if body is not None else None, headers=headers)
            r = conn.getresponse()
            raw = r.read()
        except OSError as e:
            raise LaunchError(f"连不上 Docker（{self.socket_path}）：{e}") from None
        finally:
            conn.close()
        try:
            return r.status, json.loads(raw) if raw else {}
        except ValueError:
            return r.status, {"message": raw[:300].decode("utf-8", "replace")}

    def available(self) -> bool:
        if not os.path.exists(self.socket_path):
            return False
        try:
            return self.request("GET", "/_ping", timeout=3)[0] == 200
        except LaunchError:
            return False

    def has_image(self, image: str) -> bool:
        return self.request("GET", f"/images/{quote(image, safe='')}/json")[0] == 200


def _bytes(size: str) -> int:
    units = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30}
    for u, mult in units.items():
        if size.endswith(u):
            return int(float(size[:-2]) * mult)
    return int(size)


class DockerLauncher(Launcher):
    name = "docker"
    isolated = True
    bridge_name = ""                # #123：固定的网桥网卡名（宿主机防火墙规则引用它）；空 = Docker 自动起名
    subnet = ""                     # #123：固定的沙箱网段（firewalld 按来源网段把它放进只开网关端口的 zone）

    def __init__(self, data_dir: str, image: str, network: str = "ncs-sandboxes", runtime: str = "",
                 cpu: str = "1", memory: str = "2Gi", client: DockerClient | None = None, ready_timeout: float = 60,
                 max_lifetime: int = 43200, bridge_name: str = "", subnet: str = ""):
        self.root = os.path.join(os.path.abspath(data_dir), "sandboxes")
        self.image = image
        self.network = network
        self.runtime = runtime
        self.cpu = cpu
        self.memory = memory
        self.docker = client or DockerClient()
        self.ready_timeout = ready_timeout
        self.max_lifetime = max_lifetime        # 容器最长存活秒数；Docker 没有 activeDeadlineSeconds，靠对账兜底（#60）
        self.bridge_name = bridge_name
        self.subnet = subnet
        self._gateway_ip = self._ensure_network()

    # ------------------------------------------------------------------ 网络

    def _ensure_network(self) -> str:
        status, net = self.docker.request("GET", f"/networks/{self.network}")
        if status == 404:
            body = {"Name": self.network, "Driver": "bridge", "Internal": True,      # 出不了外网，容器之间也不通
                    "Options": {ICC_OPTION: "false"}}
            if self.bridge_name:
                body["Options"][BRIDGE_NAME_OPTION] = self.bridge_name
            if self.subnet:
                body["IPAM"] = {"Config": [{"Subnet": self.subnet}]}
            status, _ = self.docker.request("POST", "/networks/create", body)
            if status not in (200, 201):
                raise LaunchError(f"创建沙箱网络失败：HTTP {status}")
            status, net = self.docker.request("GET", f"/networks/{self.network}")
        elif status != 200:
            raise LaunchError(f"读不到沙箱网络 {self.network}：HTTP {status}")
        self._check_isolation(net)
        configs = (net.get("IPAM") or {}).get("Config") or []
        gateway = next((c.get("Gateway") for c in configs if c.get("Gateway")), "")
        if not gateway:
            raise LaunchError(f"沙箱网络 {self.network} 没有网关地址")
        return gateway

    def _check_isolation(self, net: dict) -> None:
        """已存在的沙箱网络（上次部署留下的、管理员预建的、NCS_DOCKER_NETWORK 指过去
        的，包括默认的 bridge）必须真的是隔离网络，否则拒绝启动。

        只在自己创建时设 Internal/enable_icc 是不够的：预建一个普通 bridge（或
        NCS_DOCKER_NETWORK=bridge）时，沙箱就有完整公网出口，能把用户数据和密钥
        外传，还能以网桥地址访问宿主机上所有监听的服务（Redis、管理端口……）（#57）。
        宁可拒绝启动，也不带着敞口的网络跑。
        """
        if net.get("Driver") != "bridge":
            raise LaunchError(f"沙箱网络 {self.network} 的驱动是 {net.get('Driver')!r}，只支持 bridge")
        if not net.get("Internal"):
            raise LaunchError(f"沙箱网络 {self.network} 不是 internal 网络（有外网出口）。请删掉它让平台重建，"
                              f"或用 NCS_DOCKER_NETWORK 指定一个 internal 网络")
        if (net.get("Options") or {}).get(ICC_OPTION) != "false":
            raise LaunchError(f"沙箱网络 {self.network} 没有关闭容器间通信（{ICC_OPTION}=false），"
                              f"沙箱之间能互访。请删掉它让平台重建")
        # #123：internal 只挡住了"出外网"，挡不住"沙箱 → 宿主机网桥地址上的任意端口"（sshd、nginx……）。
        # 那一层靠宿主机防火墙（deploy/firewall/），规则按固定的网桥名 / 网段写：网络对不上，
        # 规则就落空，沙箱又能连宿主机所有端口，所以宁可拒绝启动。
        if self.bridge_name and (net.get("Options") or {}).get(BRIDGE_NAME_OPTION) != self.bridge_name:
            raise LaunchError(f"沙箱网络 {self.network} 的网桥名不是 {self.bridge_name}（NCS_DOCKER_BRIDGE），"
                              f"宿主机防火墙规则对不上。请删掉它让平台重建，或把 NCS_DOCKER_BRIDGE 设成它的网桥名")
        if self.subnet:
            subnets = [c.get("Subnet") for c in ((net.get("IPAM") or {}).get("Config") or [])]
            if self.subnet not in subnets:
                raise LaunchError(f"沙箱网络 {self.network} 的网段是 {subnets}，不是 {self.subnet}（NCS_DOCKER_SUBNET），"
                                  f"宿主机防火墙规则对不上。请删掉它让平台重建，或把 NCS_DOCKER_SUBNET 设成它的网段")

    def gateway_bind_host(self) -> str:
        return self._gateway_ip

    def gateway_url_for_sandbox(self, port: int) -> str:
        return f"http://{self._gateway_ip}:{port}"

    # ------------------------------------------------------------------ 容器

    @staticmethod
    def container_name(sid: str) -> str:
        return f"ncs-sbx-{sid}"

    def _dirs(self, sid: str) -> tuple[str, str, str]:
        base = os.path.join(self.root, sid)
        return base, os.path.join(base, "data"), os.path.join(base, "policy")

    def container_config(self, spec: SandboxSpec, data_dir: str, policy_dir: str) -> dict:
        env = spec.env("/data/home", "/etc/nanocode/policy.json")
        host = {
            "Binds": [f"{data_dir}:/data", f"{policy_dir}:/etc/nanocode:ro"],
            "NetworkMode": self.network,
            "ReadonlyRootfs": True,
            "Tmpfs": {"/tmp": "rw,size=512m"},
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Memory": _bytes(self.memory),
            "NanoCpus": int(float(self.cpu) * 1e9),
            "PidsLimit": 512,
        }
        if self.runtime:
            host["Runtime"] = self.runtime
        return {
            "Image": self.image,
            "Cmd": spec.args("0.0.0.0", SANDBOX_PORT, "/data/workspace"),
            "Env": [f"{k}={v}" for k, v in env.items()],
            "User": f"{sandbox_ids()[0]}:{sandbox_ids()[1]}",
            "WorkingDir": "/data/workspace",
            "ExposedPorts": {f"{SANDBOX_PORT}/tcp": {}},
            # ncs.created：对账时按它停掉超寿容器（Docker 没有 K8s 的 activeDeadlineSeconds，#60）
            "Labels": {"app": "nanocode-sandbox", "ncs.session": spec.session_id,
                       "ncs.created": str(int(time.time()))},
            "HostConfig": host,
        }

    def start(self, spec: SandboxSpec) -> str:
        sid = spec.session_id
        self.stop(sid)
        base, data_dir, policy_dir = self._dirs(sid)
        for d in (os.path.join(data_dir, "workspace"), os.path.join(data_dir, "home"), policy_dir):
            os.makedirs(d, exist_ok=True)
        with open(os.path.join(policy_dir, "policy.json"), "w", encoding="utf-8") as f:
            json.dump({**spec.policy, "audit_log": "/data/home/audit.jsonl"}, f, ensure_ascii=False, indent=1)
        uid, gid = sandbox_ids()
        if os.name != "nt" and os.geteuid() == 0:  # 容器里以 10001 运行，工作区要归它所有（Windows 无 geteuid）
            for root, dirs, files in os.walk(data_dir):
                for p in [root, *(os.path.join(root, n) for n in dirs + files)]:
                    os.chown(p, uid, gid)
        else:
            # 非 root：容器和控制面同 uid，工作区必须是当前用户能写的，否则容器里一定写不了盘（#58）。
            # 这里早报告早好，不然要等 wait_ready 超时才变成一个没有上下文的 502。
            for d in (data_dir, os.path.join(data_dir, "workspace"), os.path.join(data_dir, "home")):
                if not os.access(d, os.W_OK):
                    raise LaunchError(f"工作区 {d} 不属于当前用户（uid {uid}）或没有写权限，"
                                      "容器里写不了：请修正属主，或用 root 运行控制面")
        name = self.container_name(sid)
        status, body = self.docker.request("POST", f"/containers/create?name={name}",
                                           self.container_config(spec, data_dir, policy_dir))
        if status not in (200, 201):
            raise LaunchError(f"创建沙箱容器失败：HTTP {status} {body.get('message', '')}")
        status, body = self.docker.request("POST", f"/containers/{name}/start")
        if status not in (200, 204):
            self.stop(sid)
            raise LaunchError(f"启动沙箱容器失败：HTTP {status} {body.get('message', '')}")
        try:
            endpoint = f"http://{self._container_ip(name)}:{SANDBOX_PORT}"
            wait_ready(endpoint, spec.web_token, self.ready_timeout, alive=lambda: self.is_alive(sid))
        except LaunchError:              # 包括拿不到地址：都要把容器清理掉
            logs = self._logs(name)
            self.stop(sid)
            raise LaunchError(f"沙箱容器没有就绪。日志：{logs}") from None
        return endpoint

    def _container_ip(self, name: str) -> str:
        """控制面直接访问容器在沙箱网络里的地址：内部网络不能发布端口，也就不用在宿主机上开端口。"""
        deadline = time.time() + 10
        while time.time() < deadline:
            _, info = self.docker.request("GET", f"/containers/{name}/json")
            net = ((info.get("NetworkSettings") or {}).get("Networks") or {}).get(self.network) or {}
            if net.get("IPAddress"):
                return net["IPAddress"]
            time.sleep(0.2)
        raise LaunchError("拿不到沙箱容器的地址")

    def _logs(self, name: str) -> str:
        conn = _UnixConnection(self.docker.socket_path, 10)
        try:
            conn.request("GET", f"/v1.41/containers/{name}/logs?stdout=1&stderr=1&tail=20")
            raw = conn.getresponse().read()
        except OSError:
            return ""
        finally:
            conn.close()
        return raw.decode("utf-8", "replace")[-800:]

    def stop(self, session_id: str) -> None:
        self.docker.request("DELETE", f"/containers/{self.container_name(session_id)}?force=true")

    def delete(self, session_id: str) -> None:
        self.stop(session_id)
        shutil.rmtree(self._dirs(session_id)[0], ignore_errors=True)

    def is_alive(self, session_id: str) -> bool:
        status, info = self.docker.request("GET", f"/containers/{self.container_name(session_id)}/json")
        return status == 200 and bool((info.get("State") or {}).get("Running"))

    # ------------------------------------------------------------------ 控制面生命周期

    def _sandbox_containers(self) -> list[dict]:
        """所有带我们标签的容器（含已停止的）。按标签列，不靠内存记录——
        控制面可能刚重启，内存里什么都没有（#60）。"""
        filters = quote(json.dumps({"label": ["app=nanocode-sandbox"]}), safe="")
        status, containers = self.docker.request("GET", f"/containers/json?all=1&filters={filters}")
        if status != 200:
            raise LaunchError(f"列不出沙箱容器：HTTP {status}")
        return containers if isinstance(containers, list) else []

    def shutdown(self) -> None:
        """控制面退出：停掉所有沙箱容器（#60）。

        以前基类是空实现，Docker/K8s 启动器都不覆盖，SIGTERM 停掉控制面后
        容器还带着有效的网关令牌继续跑，只能手工清理。工作区保留，下次访问会重新拉起。
        """
        try:
            containers = self._sandbox_containers()
        except LaunchError:
            return                      # Docker 已经不在了：没什么可停的
        for c in containers:
            sid = (c.get("Labels") or {}).get("ncs.session", "")
            if sid:
                self.stop(sid)

    def reconcile(self, keep: set[str]) -> None:
        """控制面启动时：停掉 keep 之外的沙箱容器，以及存活超过 max_lifetime 的（#60）。

        keep 是数据库里还在用（starting/running）的会话。被删除/停止的会话不该留下
        容器；超寿的容器拿着过期的网关令牌也没用，停掉（保留工作区）下次带新令牌重启。
        没有 ncs.created 标签的是更早版本起的容器：不猜寿龄，只按 keep 判断。
        """
        now = time.time()
        for c in self._sandbox_containers():
            labels = c.get("Labels") or {}
            sid = labels.get("ncs.session", "")
            if not sid:
                continue
            created = int(labels.get("ncs.created") or 0)
            if sid not in keep or (created and now - created > self.max_lifetime):
                self.stop(sid)


__all__ = ["DockerLauncher", "DockerClient"]
