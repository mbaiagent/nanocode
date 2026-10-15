"""Kubernetes 启动器：Pod 清单里的每一项安全设置，以及对 API 的调用顺序（用假的 API 服务器）。"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import support  # noqa: F401

from nanocode_server.launcher import KubeClient, KubernetesLauncher, LaunchError, SandboxSpec
from support import FakeServer, make_config

SPEC = SandboxSpec("0123456789abcdef", "alice@example.com", "w" * 32, "ncs1.gw.token", resume=True)


class ManifestTest(unittest.TestCase):
    def setUp(self):
        cfg = make_config(launcher="k8s", k8s_image="registry/nanocode-sandbox:1.0", k8s_cpu="2", k8s_memory="4Gi")
        self.launcher = KubernetesLauncher(cfg, client=KubeClient("http://unused"))
        self.pod = self.launcher.pod_manifest(SPEC)
        self.spec = self.pod["spec"]
        self.c = self.spec["containers"][0]

    def test_isolation(self):
        self.assertEqual(self.spec["runtimeClassName"], "gvisor")
        self.assertFalse(self.spec["automountServiceAccountToken"])     # 沙箱里没有集群凭证
        self.assertFalse(self.spec["enableServiceLinks"])
        self.assertTrue(self.spec["securityContext"]["runAsNonRoot"])
        sc = self.c["securityContext"]
        self.assertFalse(sc["allowPrivilegeEscalation"])
        self.assertTrue(sc["readOnlyRootFilesystem"])
        self.assertEqual(sc["capabilities"]["drop"], ["ALL"])
        self.assertEqual(self.c["resources"]["limits"]["cpu"], "2")
        self.assertEqual(self.c["resources"]["limits"]["memory"], "4Gi")
        self.assertIn("ephemeral-storage", self.c["resources"]["limits"])
        self.assertEqual(self.pod["metadata"]["labels"]["app"], "nanocode-sandbox")   # NetworkPolicy 靠这个标签

    def test_credentials_and_policy(self):
        env = {e["name"]: e["value"] for e in self.c["env"]}
        self.assertEqual(env["NANOCODE_API_KEY"], "ncs1.gw.token")       # 只有网关令牌
        self.assertEqual(env["NANOCODE_WEB_TOKEN"], "w" * 32)
        self.assertEqual(env["NANOCODE_POLICY"], "/etc/nanocode/policy.json")
        mounts = {m["mountPath"]: m for m in self.c["volumeMounts"]}
        self.assertTrue(mounts["/etc/nanocode"]["readOnly"])            # 策略文件只读
        self.assertIn("--resume", self.c["args"])

    def test_no_runtime_class_if_disabled(self):
        self.launcher.cfg.k8s_runtime_class = ""
        self.assertNotIn("runtimeClassName", self.launcher.pod_manifest(SPEC)["spec"])


class FakeKube:
    """假的 Kubernetes API：记录调用，模拟 Pod 的存在、启动（先 Pending 再 Running）和终止（删除后还会存在一会儿）。"""

    def __init__(self, sandbox_port: int, fail_pod: bool = False, pod_exists: bool = False, terminating_polls: int = 0):
        self.calls = []
        self.pvc_exists = False
        self.pod_exists = pod_exists
        self.terminating = 0          # 删除后还要再被查询几次才真正消失
        self.terminating_polls = terminating_polls
        self.gets = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.calls.append(("GET", self.path))
                if "persistentvolumeclaims" in self.path:
                    return self._reply(200 if outer.pvc_exists else 404, {})
                if outer.terminating > 0:
                    outer.terminating -= 1
                    return self._reply(200, {"status": {"phase": "Running"}, "metadata": {"deletionTimestamp": "x"}})
                if not outer.pod_exists:
                    return self._reply(404, {})
                outer.gets += 1
                if fail_pod:
                    return self._reply(200, {"status": {"phase": "Failed", "message": "OOMKilled"}})
                if outer.gets == 1:
                    return self._reply(200, {"status": {"phase": "Pending"}})
                return self._reply(200, {"status": {"phase": "Running", "podIP": "127.0.0.1",
                                                    "conditions": [{"type": "Ready", "status": "True"}]}})

            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(n))
                outer.calls.append(("POST", self.path, body))
                if body["kind"] == "PersistentVolumeClaim":
                    outer.pvc_exists = True
                elif outer.terminating > 0:
                    return self._reply(409, {"message": "object is being deleted"})
                else:
                    outer.pod_exists = True
                self._reply(201, body)

            def do_DELETE(self):
                outer.calls.append(("DELETE", self.path))
                if "pods" in self.path:
                    existed = outer.pod_exists
                    outer.pod_exists = False
                    if existed:
                        outer.terminating = outer.terminating_polls
                    return self._reply(200 if existed else 404, {})
                self._reply(200, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class LauncherFlowTest(unittest.TestCase):
    def setUp(self):
        # 假沙箱：只要 /api/state 带对令牌就返回 200
        self.sandbox = FakeServer(lambda m, p, h, b: (200 if h.get("x-nanocode-token") == "w" * 32 else 403,
                                                      {"content-type": "application/json"}, b"{}"))

    def tearDown(self):
        self.sandbox.close()

    def launcher(self, kube):
        import nanocode_server.launcher as mod
        self.orig_port = mod.SANDBOX_PORT
        mod.SANDBOX_PORT = self.sandbox.port      # 让"Pod IP:端口"指到假沙箱
        self.addCleanup(setattr, mod, "SANDBOX_PORT", self.orig_port)
        return KubernetesLauncher(make_config(launcher="k8s", k8s_namespace="sbx"), client=KubeClient(kube.url),
                                  ready_timeout=10, poll=0.05)

    def test_start_creates_volume_then_pod_and_waits_until_ready(self):
        kube = FakeKube(self.sandbox.port)
        try:
            endpoint = self.launcher(kube).start(SPEC)
            self.assertEqual(endpoint, f"http://127.0.0.1:{self.sandbox.port}")
            posts = [c for c in kube.calls if c[0] == "POST"]
            self.assertEqual([p[2]["kind"] for p in posts], ["PersistentVolumeClaim", "Pod"])
            self.assertTrue(all(p[1].startswith("/api/v1/namespaces/sbx/") for p in posts))
        finally:
            kube.close()

    def test_restart_waits_for_the_old_pod_to_terminate(self):
        """重启沙箱：旧 Pod 还在终止时创建同名 Pod 会 409，必须等它真正消失。"""
        kube = FakeKube(self.sandbox.port, pod_exists=True, terminating_polls=3)
        try:
            endpoint = self.launcher(kube).start(SPEC)
            self.assertTrue(endpoint.endswith(str(self.sandbox.port)))
            pod_posts = [c for c in kube.calls if c[0] == "POST" and c[2]["kind"] == "Pod"]
            self.assertEqual(len(pod_posts), 1)            # 没有撞上 409 再重试，一次就创建成功
        finally:
            kube.close()

    def test_failed_pod_raises(self):
        kube = FakeKube(self.sandbox.port, fail_pod=True)
        try:
            with self.assertRaises(LaunchError):
                self.launcher(kube).start(SPEC)
        finally:
            kube.close()

    def test_stop_keeps_workspace_delete_removes_it(self):
        kube = FakeKube(self.sandbox.port)
        try:
            launcher = self.launcher(kube)
            launcher.stop(SPEC.session_id)
            self.assertFalse(any("persistentvolumeclaims" in c[1] for c in kube.calls if c[0] == "DELETE"))
            launcher.delete(SPEC.session_id)
            self.assertTrue(any("persistentvolumeclaims" in c[1] for c in kube.calls if c[0] == "DELETE"))
        finally:
            kube.close()


if __name__ == "__main__":
    unittest.main()
