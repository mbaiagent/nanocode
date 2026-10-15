"""沙箱启动器：K8s 的清单和调用顺序（假的 API 服务器）、Docker 的容器设置（以及真的启动，可选）、本机进程。"""

import json
import os
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import support  # noqa: F401

from nanocode_server.runtime import (DockerClient, DockerLauncher, KubeClient, KubernetesLauncher, LaunchError,
                                     LocalLauncher, SandboxSpec)
from support import DOCKER, FakeServer, make_config

POLICY = {"provider": "custom", "model": "m", "allowed_modes": ["default"], "mode": "default"}
SPEC = SandboxSpec("0123456789abcdef", "alice", "w" * 32, "gateway-token", POLICY, resume=True)


def k8s(client=None, **kw):
    return KubernetesLauncher(client or KubeClient("http://unused"), "sbx", "registry/sandbox:1", cpu="2", memory="4Gi", **kw)


class KubernetesManifestTest(unittest.TestCase):
    def setUp(self):
        self.pod = k8s().pod_manifest(SPEC)
        self.c = self.pod["spec"]["containers"][0]

    def test_isolation(self):
        spec = self.pod["spec"]
        self.assertEqual(spec["runtimeClassName"], "gvisor")
        self.assertFalse(spec["automountServiceAccountToken"])       # 沙箱里没有集群凭证
        self.assertFalse(spec["enableServiceLinks"])
        self.assertTrue(spec["securityContext"]["runAsNonRoot"])
        sc = self.c["securityContext"]
        self.assertEqual((sc["allowPrivilegeEscalation"], sc["readOnlyRootFilesystem"], sc["capabilities"]["drop"]),
                         (False, True, ["ALL"]))
        self.assertEqual((self.c["resources"]["limits"]["cpu"], self.c["resources"]["limits"]["memory"]), ("2", "4Gi"))
        self.assertEqual(self.pod["metadata"]["labels"]["app"], "nanocode-sandbox")    # NetworkPolicy 靠这个标签

    def test_credentials_and_policy(self):
        env = {e["name"]: e["value"] for e in self.c["env"]}
        self.assertEqual(env["NANOCODE_API_KEY"], "gateway-token")    # 只有网关令牌
        self.assertEqual(env["NANOCODE_POLICY"], "/etc/nanocode/policy.json")
        mounts = {m["mountPath"]: m for m in self.c["volumeMounts"]}
        self.assertTrue(mounts["/etc/nanocode"]["readOnly"])
        volumes = {v["name"]: v for v in self.pod["spec"]["volumes"]}
        self.assertEqual(volumes["policy"]["configMap"]["name"], "ncs-policy-0123456789abcdef")   # 每个会话自己的策略
        cm = k8s().policy_manifest(SPEC)
        self.assertEqual(json.loads(cm["data"]["policy.json"])["model"], "m")
        self.assertIn("--resume", self.c["args"])

    def test_no_runtime_class_when_disabled(self):
        self.assertNotIn("runtimeClassName", k8s(runtime_class="").pod_manifest(SPEC)["spec"])


class FakeKube:
    """假的 K8s API：模拟 Pod 的存在、启动（先 Pending 再 Running）和终止（删除后还要被查几次才消失）。"""

    def __init__(self, fail_pod=False, pod_exists=False, terminating_polls=0):
        self.calls, self.pvc, self.pod, self.terminating, self.gets = [], False, pod_exists, 0, 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.calls.append(("GET", self.path))
                if "persistentvolumeclaims" in self.path:
                    return self.reply(200 if outer.pvc else 404, {})
                if outer.terminating > 0:
                    outer.terminating -= 1
                    return self.reply(200, {"status": {"phase": "Running"}})
                if not outer.pod:
                    return self.reply(404, {})
                outer.gets += 1
                if fail_pod:
                    return self.reply(200, {"status": {"phase": "Failed", "message": "OOMKilled"}})
                if outer.gets == 1:
                    return self.reply(200, {"status": {"phase": "Pending"}})
                return self.reply(200, {"status": {"phase": "Running", "podIP": "127.0.0.1",
                                                   "conditions": [{"type": "Ready", "status": "True"}]}})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
                outer.calls.append(("POST", self.path, body["kind"]))
                if body["kind"] == "PersistentVolumeClaim":
                    outer.pvc = True
                elif body["kind"] == "Pod":
                    if outer.terminating > 0:
                        return self.reply(409, {"message": "being deleted"})
                    outer.pod = True
                self.reply(201, body)

            def do_DELETE(self):
                outer.calls.append(("DELETE", self.path))
                if "/pods/" in self.path:
                    existed, outer.pod = outer.pod, False
                    if existed:
                        outer.terminating = terminating_polls
                    return self.reply(200 if existed else 404, {})
                self.reply(200, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class KubernetesFlowTest(unittest.TestCase):
    def setUp(self):
        self.sandbox = FakeServer(lambda m, p, h, b: (200 if h.get("x-nanocode-token") == "w" * 32 else 403,
                                                      {"content-type": "application/json"}, b"{}"))
        import nanocode_server.runtime.kubernetes as mod
        self.addCleanup(setattr, mod, "SANDBOX_PORT", mod.SANDBOX_PORT)
        mod.SANDBOX_PORT = self.sandbox.port          # 让"Pod IP:端口"指到假沙箱

    def tearDown(self):
        self.sandbox.close()

    def launcher(self, kube):
        return k8s(KubeClient(kube.url), ready_timeout=10, poll=0.05)

    def test_start_creates_volume_policy_then_pod(self):
        kube = FakeKube()
        try:
            self.assertTrue(self.launcher(kube).start(SPEC).endswith(f":{self.sandbox.port}"))
            self.assertEqual([c[2] for c in kube.calls if c[0] == "POST"], ["PersistentVolumeClaim", "ConfigMap", "Pod"])
            self.assertTrue(all(c[1].startswith("/api/v1/namespaces/sbx/") for c in kube.calls))
        finally:
            kube.close()

    def test_restart_waits_for_old_pod(self):
        kube = FakeKube(pod_exists=True, terminating_polls=3)
        try:
            self.launcher(kube).start(SPEC)
            self.assertEqual(sum(1 for c in kube.calls if c[0] == "POST" and c[2] == "Pod"), 1)   # 没有撞上 409
        finally:
            kube.close()

    def test_failed_pod_raises(self):
        kube = FakeKube(fail_pod=True)
        try:
            with self.assertRaises(LaunchError):
                self.launcher(kube).start(SPEC)
        finally:
            kube.close()

    def test_stop_keeps_workspace_delete_removes_it(self):
        kube = FakeKube()
        try:
            launcher = self.launcher(kube)
            launcher.stop(SPEC.session_id)
            self.assertFalse(any("persistentvolumeclaims" in c[1] for c in kube.calls if c[0] == "DELETE"))
            launcher.delete(SPEC.session_id)
            deleted = [c[1] for c in kube.calls if c[0] == "DELETE"]
            self.assertTrue(any("persistentvolumeclaims" in p for p in deleted))
            self.assertTrue(any("configmaps" in p for p in deleted))
        finally:
            kube.close()


class DockerConfigTest(unittest.TestCase):
    def test_container_is_locked_down(self):
        launcher = DockerLauncher.__new__(DockerLauncher)     # 不连 Docker，只看生成的容器配置
        launcher.image, launcher.network, launcher.runtime, launcher.cpu, launcher.memory = "img", "ncs-sbx", "runsc", "1.5", "512Mi"
        c = launcher.container_config(SPEC, "/host/data", "/host/policy")
        h = c["HostConfig"]
        self.assertEqual(c["User"], "10001:10001")
        self.assertTrue(h["ReadonlyRootfs"])
        self.assertEqual(h["CapDrop"], ["ALL"])
        self.assertIn("no-new-privileges", h["SecurityOpt"])
        self.assertEqual((h["Memory"], h["NanoCpus"], h["Runtime"], h["NetworkMode"]), (512 * 2**20, 1_500_000_000, "runsc", "ncs-sbx"))
        self.assertIn("/host/policy:/etc/nanocode:ro", h["Binds"])
        self.assertNotIn("PortBindings", h)                   # 不在宿主机上开端口
        env = dict(e.split("=", 1) for e in c["Env"])
        self.assertEqual(env["NANOCODE_API_KEY"], "gateway-token")


@unittest.skipUnless(DOCKER, "设置 NCS_TEST_DOCKER=1 才真的启动 Docker 沙箱")
class DockerRealTest(unittest.TestCase):
    def test_start_stop_and_isolation(self):
        cfg = make_config()
        launcher = DockerLauncher(cfg.data_dir, "nanocode-sandbox:latest", "ncs-sandboxes-test")
        spec = SandboxSpec("fedcba9876543210", "alice", "w" * 32, "", {"provider": "mock", "model": "mock-demo",
                                                                         "allowed_modes": ["default"], "mode": "default"})
        try:
            endpoint = launcher.start(spec)
            req = urllib.request.Request(endpoint + "/api/state", headers={"x-nanocode-token": "w" * 32, "Host": "localhost"})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertEqual(json.loads(r.read())["provider"], "mock")
            self.assertTrue(launcher.is_alive(spec.session_id))
            self.assertTrue(os.path.isdir(os.path.join(cfg.data_dir, "sandboxes", spec.session_id, "data", "home")))
        finally:
            launcher.delete(spec.session_id)
        self.assertFalse(launcher.is_alive(spec.session_id))

    def test_client_ping(self):
        self.assertTrue(DockerClient().available())


class GatewayUrlTest(unittest.TestCase):
    """沙箱访问网关的地址：明确配置的优先（网关单独部署时），否则用启动器给的（Docker 的网桥地址）。"""

    def manager(self, cfg):
        from nanocode_server.security.secretbox import SecretBox
        from nanocode_server.services.catalog import ModelCatalog
        from nanocode_server.services.sessions import SessionManager
        from nanocode_server.storage import open_store

        class Bridge(LocalLauncher):
            def gateway_url_for_sandbox(self, port):
                return f"http://172.18.0.1:{port}"
        store = open_store(cfg)
        self.addCleanup(store.close)
        return SessionManager(cfg, store, Bridge(cfg.data_dir), ModelCatalog(cfg, store, SecretBox(cfg.secret_key)))

    def test_explicit_url_wins(self):
        self.assertEqual(self.manager(make_config(gateway_url="http://gw.internal:18701/")).gateway_url,
                         "http://gw.internal:18701")

    def test_launcher_suggestion_otherwise(self):
        self.assertEqual(self.manager(make_config(gateway_port=8701)).gateway_url, "http://172.18.0.1:8701")


class LocalLauncherTest(unittest.TestCase):
    def test_refuses_bypass(self):
        """本机子进程没有隔离，不能允许 bypass 模式。"""
        spec = SandboxSpec("0" * 16, "a", "w" * 32, "", {**POLICY, "allowed_modes": ["default", "bypass"]})
        with self.assertRaises(LaunchError):
            LocalLauncher(make_config().data_dir).start(spec)


if __name__ == "__main__":
    unittest.main()
