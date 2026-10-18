"""Kubernetes：每个会话一个 Pod（生产部署）。

隔离：gVisor 运行时、非 root、只读根文件系统、去掉全部 capabilities、不挂载集群凭证、
不注入集群服务地址、有最长存活时间。网络出口由 NetworkPolicy 控制（deploy/k8s/30-network-policy.yaml）。
存储：每个会话一个持久卷（工作区 + NANOCODE_HOME），沙箱回收后再启动可以继续；
平台策略放在每个会话自己的 ConfigMap 里，只读挂载（每个会话可以选不同的模型）。
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request

from .base import SANDBOX_PORT, Launcher, LaunchError, SandboxSpec, wait_ready

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


def in_cluster() -> bool:
    return os.path.isfile(os.path.join(SA_DIR, "token"))


class KubeClient:
    """极简的 Kubernetes API 客户端：Pod、PVC、ConfigMap 的增删查。集群内用 ServiceAccount 认证。"""

    def __init__(self, api: str, token: str = "", ca_file: str | None = None):
        self.api = api.rstrip("/")
        self.token = token
        self.ctx = ssl.create_default_context(cafile=ca_file) if api.startswith("https") else None

    @classmethod
    def in_cluster(cls, api: str) -> "KubeClient":
        with open(os.path.join(SA_DIR, "token"), encoding="utf-8") as f:
            token = f.read().strip()
        return cls(api, token, os.path.join(SA_DIR, "ca.crt"))

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        headers = {"accept": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["content-type"] = "application/json"
        req = urllib.request.Request(self.api + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15, context=self.ctx) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
        except (urllib.error.URLError, OSError) as e:
            raise LaunchError(f"连不上 Kubernetes API：{e}") from None


class KubernetesLauncher(Launcher):
    name = "k8s"
    isolated = True

    def __init__(self, client: KubeClient, namespace: str, image: str, runtime_class: str = "gvisor",
                 storage_class: str = "", workspace_size: str = "10Gi", cpu: str = "1", memory: str = "2Gi",
                 max_lifetime: int = 43200, ready_timeout: float = 120, poll: float = 1.0):
        self.kube = client
        self.ns = namespace
        self.image = image
        self.runtime_class = runtime_class
        self.storage_class = storage_class
        self.workspace_size = workspace_size
        self.cpu = cpu
        self.memory = memory
        self.max_lifetime = max_lifetime
        self.ready_timeout = ready_timeout
        self.poll = poll

    @staticmethod
    def names(sid: str) -> dict:
        return {"pod": f"ncs-sbx-{sid}", "pvc": f"ncs-ws-{sid}", "policy": f"ncs-policy-{sid}"}

    def _path(self, kind: str, name: str = "") -> str:
        return f"/api/v1/namespaces/{self.ns}/{kind}" + (f"/{name}" if name else "")

    # ------------------------------------------------------------------ 清单

    def pvc_manifest(self, sid: str) -> dict:
        pvc = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
               "metadata": {"name": self.names(sid)["pvc"], "labels": {"app": "nanocode-sandbox", "ncs/session": sid}},
               "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": self.workspace_size}}}}
        if self.storage_class:
            pvc["spec"]["storageClassName"] = self.storage_class
        return pvc

    def policy_manifest(self, spec: SandboxSpec) -> dict:
        policy = {**spec.policy, "audit_log": "/data/home/audit.jsonl"}
        return {"apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": self.names(spec.session_id)["policy"],
                             "labels": {"app": "nanocode-sandbox", "ncs/session": spec.session_id}},
                "data": {"policy.json": json.dumps(policy, ensure_ascii=False, indent=1)}}

    def pod_manifest(self, spec: SandboxSpec) -> dict:
        n = self.names(spec.session_id)
        hardened = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
        env = spec.env("/data/home", "/etc/nanocode/policy.json")
        pod = {
            "apiVersion": "v1", "kind": "Pod",
            # NetworkPolicy 按 app 标签限制：只有控制面能访问沙箱，沙箱只能访问网关
            "metadata": {"name": n["pod"], "labels": {"app": "nanocode-sandbox", "ncs/session": spec.session_id}},
            "spec": {
                "automountServiceAccountToken": False,
                "enableServiceLinks": False,
                "restartPolicy": "Never",
                "activeDeadlineSeconds": self.max_lifetime,
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001,
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "initContainers": [{
                    "name": "init-dirs", "image": self.image, "command": ["sh", "-c", "mkdir -p /data/workspace /data/home"],
                    "securityContext": hardened, "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                    "resources": {"limits": {"cpu": "100m", "memory": "64Mi"}},
                }],
                "containers": [{
                    "name": "nanocode", "image": self.image,
                    "args": spec.args("0.0.0.0", SANDBOX_PORT, "/data/workspace"),
                    "env": [{"name": k, "value": v} for k, v in env.items()],
                    "ports": [{"containerPort": SANDBOX_PORT, "name": "web"}],
                    "readinessProbe": {"tcpSocket": {"port": SANDBOX_PORT}, "periodSeconds": 2},
                    "resources": {"requests": {"cpu": "250m", "memory": "512Mi", "ephemeral-storage": "1Gi"},
                                  "limits": {"cpu": self.cpu, "memory": self.memory, "ephemeral-storage": "2Gi"}},
                    "securityContext": hardened,
                    "volumeMounts": [{"name": "data", "mountPath": "/data"},
                                     {"name": "policy", "mountPath": "/etc/nanocode", "readOnly": True},
                                     {"name": "tmp", "mountPath": "/tmp"}],
                }],
                "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": n["pvc"]}},
                            {"name": "policy", "configMap": {"name": n["policy"]}},
                            {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}],
            },
        }
        if self.runtime_class:
            pod["spec"]["runtimeClassName"] = self.runtime_class
        return pod

    # ------------------------------------------------------------------ 生命周期

    def _create(self, kind: str, manifest: dict, ok_if_exists: bool = False) -> None:
        status, body = self.kube.request("POST", self._path(kind), manifest)
        if status in (200, 201) or (ok_if_exists and status == 409):
            return
        raise LaunchError(f"创建 {kind} 失败：HTTP {status} {body.get('message', '')}")

    def start(self, spec: SandboxSpec) -> str:
        sid = spec.session_id
        n = self.names(sid)
        if self.kube.request("GET", self._path("persistentvolumeclaims", n["pvc"]))[0] == 404:
            self._create("persistentvolumeclaims", self.pvc_manifest(sid), ok_if_exists=True)
        self.kube.request("DELETE", self._path("configmaps", n["policy"]))
        self._create("configmaps", self.policy_manifest(spec))
        if self._delete_pod(sid):
            self._wait_gone(sid)            # 旧 Pod 还在终止时创建同名 Pod 会 409
        self._create("pods", self.pod_manifest(spec))
        deadline = time.time() + self.ready_timeout
        while time.time() < deadline:
            status, pod = self.kube.request("GET", self._path("pods", n["pod"]))
            st = pod.get("status", {}) if status == 200 else {}
            if st.get("phase") in ("Failed", "Succeeded"):
                raise LaunchError(f"沙箱 Pod 已退出（{st['phase']}）：{st.get('message', '')}")
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in st.get("conditions", []))
            if st.get("phase") == "Running" and ready and st.get("podIP"):
                endpoint = f"http://{st['podIP']}:{SANDBOX_PORT}"
                wait_ready(endpoint, spec.web_token, max(5.0, deadline - time.time()))
                return endpoint
            time.sleep(self.poll)
        self._delete_pod(sid)
        raise LaunchError(f"沙箱 Pod {self.ready_timeout:.0f} 秒内没有就绪")

    def _delete_pod(self, sid: str) -> bool:
        status, _ = self.kube.request("DELETE", self._path("pods", self.names(sid)["pod"]) + "?gracePeriodSeconds=5")
        return status in (200, 202)

    def _wait_gone(self, sid: str, timeout: float = 60) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.kube.request("GET", self._path("pods", self.names(sid)["pod"]))[0] == 404:
                return
            time.sleep(self.poll)
        raise LaunchError(f"旧的沙箱 Pod {timeout:.0f} 秒内没有删除完")

    def stop(self, session_id: str) -> None:
        self._delete_pod(session_id)

    def delete(self, session_id: str) -> None:
        n = self.names(session_id)
        self._delete_pod(session_id)
        self.kube.request("DELETE", self._path("configmaps", n["policy"]))
        self.kube.request("DELETE", self._path("persistentvolumeclaims", n["pvc"]))

    def is_alive(self, session_id: str) -> bool:
        status, pod = self.kube.request("GET", self._path("pods", self.names(session_id)["pod"]))
        return status == 200 and pod.get("status", {}).get("phase") in ("Pending", "Running")

    # ------------------------------------------------------------------ 控制面生命周期

    def _sandbox_pods(self) -> list[dict]:
        status, pods = self.kube.request("GET", self._path("pods") + "?labelSelector=app%3Dnanocode-sandbox")
        if status != 200:
            raise LaunchError(f"列不出沙箱 Pod：HTTP {status}")
        return pods.get("items", [])

    def shutdown(self) -> None:
        """控制面退出：删掉所有沙箱 Pod（#60）。

        以前基类是空实现，滚动更新 / docker stop 控制面后 Pod 还带着有效网关令牌继续跑。
        PVC 保留：工作区还要用于恢复对话，下次访问会重建 Pod。
        """
        try:
            pods = self._sandbox_pods()
        except LaunchError:
            return                      # API 已经不在了：Pod 大概率也没了
        for pod in pods:
            sid = (pod.get("metadata") or {}).get("labels", {}).get("ncs/session", "")
            if sid:
                self.stop(sid)

    def reconcile(self, keep: set[str]) -> None:
        """控制面启动时：删掉 keep 之外的沙箱 Pod（#60）。

        寿龄有 activeDeadlineSeconds 兜底，这里只管"数据库里已经没有会话"的无主 Pod。
        PVC 保留，会话下次访问会重建 Pod。API 连不上就让它抛出来：控制面的核心职责
        就是管沙箱，后端不可用时宁可起不来（和上面 is_alive 的行为一致）。
        """
        for pod in self._sandbox_pods():
            sid = (pod.get("metadata") or {}).get("labels", {}).get("ncs/session", "")
            if sid and sid not in keep:
                self.stop(sid)


__all__ = ["KubernetesLauncher", "KubeClient", "in_cluster"]
