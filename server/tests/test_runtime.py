"""沙箱启动器：K8s 的清单和调用顺序（假的 API 服务器）、Docker 的容器设置（以及真的启动，可选）、本机进程。"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler
from unittest import mock

import support  # noqa: F401

from nanocode_server.runtime import (DockerClient, DockerLauncher, KubeClient, KubernetesLauncher, LaunchError,
                                     LocalLauncher, SandboxSpec)
from nanocode_server.runtime.docker import sandbox_ids
from support import DOCKER, FakeServer, LocalHTTPServer, make_config

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
                if "/pods?" in self.path:                 # 带 labelSelector 的列表（shutdown/reconcile 用）
                    items = ([{"metadata": {"name": f"ncs-sbx-{SPEC.session_id}",
                                            "labels": {"ncs/session": SPEC.session_id}}}]
                             if outer.pod else [])
                    return self.reply(200, {"items": items})
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

        self.server = LocalHTTPServer(("127.0.0.1", 0), H)
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
        # 容器 uid:gid 跟着控制面身份走：root 时是 10001（并 chown 工作区），
        # 普通用户时就是控制面自己——否则容器里写不了工作区（#58）
        uid, gid = sandbox_ids()
        self.assertEqual(c["User"], f"{uid}:{gid}")
        self.assertNotEqual(uid, 0)                           # 任何情况下容器都不以 root 跑
        self.assertIn("ncs.created", c["Labels"])             # 对账时按它停掉超寿容器（#60）
        self.assertTrue(h["ReadonlyRootfs"])
        self.assertEqual(h["CapDrop"], ["ALL"])
        self.assertIn("no-new-privileges", h["SecurityOpt"])
        self.assertEqual((h["Memory"], h["NanoCpus"], h["Runtime"], h["NetworkMode"]), (512 * 2**20, 1_500_000_000, "runsc", "ncs-sbx"))
        self.assertIn("/host/policy:/etc/nanocode:ro", h["Binds"])
        self.assertNotIn("PortBindings", h)                   # 不在宿主机上开端口
        env = dict(e.split("=", 1) for e in c["Env"])
        self.assertEqual(env["NANOCODE_API_KEY"], "gateway-token")


class DockerNetworkTest(unittest.TestCase):
    """#57：已存在的沙箱网络必须真的是隔离网络（internal + 容器间不通 + bridge），否则拒绝启动。"""

    INTERNAL = {"Driver": "bridge", "Internal": True,
                "Options": {"com.docker.network.bridge.enable_icc": "false"},
                "IPAM": {"Config": [{"Gateway": "172.18.0.1"}]}}

    def launcher(self, net=None):
        class FakeDocker:
            def __init__(self):
                self.net, self.created = net, None

            def request(self, method, path, body=None, timeout=60):
                if method == "GET" and path.startswith("/networks/"):
                    return (200, self.net) if self.net is not None else (404, {})
                if method == "POST" and path == "/networks/create":
                    self.created = body
                    self.net = {"Driver": "bridge", "Internal": True,
                                "Options": {"com.docker.network.bridge.enable_icc": "false"},
                                "IPAM": {"Config": [{"Gateway": "172.19.0.1"}]}}
                    return 201, {}
                return 200, {}

        d = DockerLauncher.__new__(DockerLauncher)        # 不连 Docker，只测网络校验
        d.network, d.docker = "ncs-sandboxes", FakeDocker()
        return d

    def test_accepts_internal_network(self):
        self.assertEqual(self.launcher(self.INTERNAL)._ensure_network(), "172.18.0.1")

    def test_rejects_networks_without_isolation(self):
        for bad in ({**self.INTERNAL, "Internal": False},                    # 普通 bridge：有公网出口
                    {**self.INTERNAL, "Options": {}},                        # 没关 icc：容器之间能互访
                    {**self.INTERNAL, "Options": {"com.docker.network.bridge.enable_icc": "true"}},
                    {**self.INTERNAL, "Driver": "overlay"}):                 # 非 bridge 驱动
            with self.subTest(net=bad):
                with self.assertRaises(LaunchError):
                    self.launcher(bad)._ensure_network()

    def test_missing_network_is_created_as_internal(self):
        d = self.launcher(None)                                               # 404：平台自己创建
        self.assertEqual(d._ensure_network(), "172.19.0.1")
        self.assertEqual(d.docker.created["Internal"], True)
        self.assertEqual(d.docker.created["Options"]["com.docker.network.bridge.enable_icc"], "false")

    def test_network_without_gateway_address_is_refused(self):
        with self.assertRaises(LaunchError):
            self.launcher({**self.INTERNAL, "IPAM": {}})._ensure_network()


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


class ReadyErrorMessageTest(unittest.TestCase):
    """沙箱起不来时，报错要能说明白为什么，同时不能把沙箱令牌漏出去（#92）。"""

    def test_log_tail_is_included_and_token_redacted(self):
        from nanocode_server.runtime.base import _log_tail
        d = tempfile.mkdtemp()
        path = os.path.join(d, "sandbox.log")
        with open(path, "w", encoding="utf-8") as f:
            f.write("启动 http://127.0.0.1:9/?token=SUPERSECRET\nTraceback: boom\n")
        tail = _log_tail(path)
        self.assertIn("Traceback: boom", tail)
        self.assertNotIn("SUPERSECRET", tail)
        self.assertIn("token=***", tail)

    def test_missing_or_empty_log_is_silent(self):
        from nanocode_server.runtime.base import _log_tail
        self.assertEqual(_log_tail(""), "")
        self.assertEqual(_log_tail("/definitely/not/here.log"), "")


class LocalLauncherTest(unittest.TestCase):
    def test_refuses_bypass(self):
        """本机子进程没有隔离，不能允许 bypass 模式。"""
        spec = SandboxSpec("0" * 16, "a", "w" * 32, "", {**POLICY, "allowed_modes": ["default", "bypass"]})
        with self.assertRaises(LaunchError):
            LocalLauncher(make_config().data_dir).start(spec)

    def _fake_proc(self):
        proc = mock.Mock(pid=4321)
        proc.poll.return_value = None
        proc.wait.return_value = 0
        return proc

    def test_stop_kills_the_process_group_on_posix(self):
        """#75：沙箱是 start_new_session=True 起的，POSIX 上必须杀整个进程组。"""
        launcher = LocalLauncher(tempfile.mkdtemp(prefix="ncs-stop-"))
        proc = self._fake_proc()
        launcher.procs["s"] = proc
        with mock.patch("os.name", "posix"), \
                mock.patch("signal.SIGKILL", 9, create=True), \
                mock.patch("os.killpg", create=True) as killpg, \
                mock.patch("os.getpgid", return_value=4321, create=True):
            launcher.stop("s")
        killpg.assert_called_once()
        self.assertEqual(killpg.call_args[0][0], 4321)
        proc.terminate.assert_not_called()

    def test_stop_uses_terminate_on_windows(self):
        """Windows 没有 SIGKILL / killpg：退回 terminate/kill。"""
        launcher = LocalLauncher(tempfile.mkdtemp(prefix="ncs-stop-"))
        proc = self._fake_proc()
        launcher.procs["s"] = proc
        with mock.patch("os.name", "nt"):
            launcher.stop("s")
        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()


class FakeDockerClient:
    """假 Docker API：只会列容器和删容器（shutdown / reconcile 用）。"""

    def __init__(self, containers):
        self.containers = list(containers)
        self.deleted = []

    def request(self, method, path, body=None, timeout=60):
        if method == "GET" and path.startswith("/containers/json"):
            return 200, self.containers
        if method == "DELETE":
            name = path[len("/containers/"):].split("?", 1)[0]
            self.deleted.append(name)
            self.containers = [c for c in self.containers
                               if not any(n.lstrip("/") == name for n in c.get("Names", []))]
            return 204, {}
        raise AssertionError(f"假 Docker 不认识的调用：{method} {path}")


class DockerLifecycleTest(unittest.TestCase):
    """#60：控制面退出/重启时，Docker 沙箱容器要被清掉，不能带着有效令牌继续跑。"""

    def launcher(self, containers):
        launcher = DockerLauncher.__new__(DockerLauncher)
        launcher.docker = FakeDockerClient(containers)
        launcher.max_lifetime = 43200
        return launcher

    @staticmethod
    def container(sid, created=None):
        labels = {"app": "nanocode-sandbox", "ncs.session": sid}
        if created is not None:
            labels["ncs.created"] = str(int(created))
        return {"Names": [f"/ncs-sbx-{sid}"], "Labels": labels}

    def test_shutdown_stops_all_sandbox_containers(self):
        launcher = self.launcher([self.container("a" * 16), self.container("b" * 16),
                                  {"Names": ["/someone-elses"], "Labels": {"app": "other"}}])
        launcher.shutdown()
        self.assertEqual(sorted(launcher.docker.deleted), sorted(["ncs-sbx-" + "a" * 16, "ncs-sbx-" + "b" * 16]))

    def test_reconcile_stops_orphans_and_expired(self):
        now = time.time()
        keep, orphan, old, noage = "k" * 16, "o" * 16, "d" * 16, "n" * 16
        launcher = self.launcher([
            self.container(keep, now), self.container(orphan, now),
            self.container(old, now - 100000),          # 超寿：会话还在也停掉
            self.container(noage),                      # 更早的版本起的：没有 created 就不猜寿龄
        ])
        launcher.reconcile({keep, noage})
        self.assertEqual(sorted(launcher.docker.deleted),
                         sorted([f"ncs-sbx-{orphan}", f"ncs-sbx-{old}"]))


class KubernetesLifecycleTest(unittest.TestCase):
    """#60：K8s 启动器同样要实现 shutdown / reconcile（以前基类是空实现）。"""

    def launcher(self, kube):
        return k8s(KubeClient(kube.url), ready_timeout=10, poll=0.05)

    def test_shutdown_deletes_all_sandbox_pods(self):
        kube = FakeKube(pod_exists=True)
        try:
            self.launcher(kube).shutdown()
            deleted = [c[1] for c in kube.calls if c[0] == "DELETE" and "/pods/" in c[1]]
            self.assertEqual(len(deleted), 1)
            self.assertIn(SPEC.session_id, deleted[0])
        finally:
            kube.close()

    def test_reconcile_removes_pods_without_a_session(self):
        kube = FakeKube(pod_exists=True)
        try:
            self.launcher(kube).reconcile(set())                # 数据库里没有会话：Pod 全清
            self.assertTrue(any(c[0] == "DELETE" and "/pods/" in c[1] for c in kube.calls))
            kube.calls.clear()
            self.launcher(kube).reconcile({SPEC.session_id})    # 会话还在：不动
            self.assertFalse(any(c[0] == "DELETE" and "/pods/" in c[1] for c in kube.calls))
        finally:
            kube.close()


class _FakeSandbox(LocalHTTPServer):
    """假沙箱：只认带对令牌的 /api/state，用来验证 pid 文件里的令牌校验。"""

    def __init__(self, token: str):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                ok = self.headers.get("x-nanocode-token") == outer.token
                self.send_response(200 if ok else 403)
                self.end_headers()
                self.wfile.write(b"{}")

        super().__init__(("127.0.0.1", 0), H)
        self.token = token
        self.port = self.server_address[1]


def _kill_later(proc):
    def cleanup():
        if proc.poll() is None:
            proc.kill()
    return cleanup


def _dummy_process():
    # 必须 start_new_session：真沙箱也是这么起的，killpg 才杀得到它自己那一组。
    # 不分组的话它和 pytest 同一个进程组，测试里的 killpg 会把测试自己一起杀掉。
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)


def _waited(proc, timeout=20):
    """等进程退出；等不到就带着状态自己 fail，别抛一个没上下文的 TimeoutExpired。

    宽限期给得比启动器的（SIGTERM 后 5 秒升级 SIGKILL）宽：慢机器/高负载下
    沙箱收到 SIGTERM 到真正退出之间可能要好几秒。
    """
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AssertionError(
            f"pid {proc.pid} 在 {timeout} 秒内没有退出（poll={proc.poll()}）—— 停沙箱的信号没送到？") from None
    return proc.returncode


class LocalPidFileTest(unittest.TestCase):
    """#60：控制面重启后内存字典是空的，但沙箱进程还活着——要凭着 pid 文件找回来，
    不能判成死再起一个（旧进程就成了没人管的孤儿）。"""

    def setUp(self):
        self.sandbox = _FakeSandbox("tok-123")
        # 必须真的 serve：_alive_pid 要连过去问 /api/state。只 shutdown 不 serve_forever
        # 会永远等在那个事件上（server_close 才是关监听）。
        threading.Thread(target=self.sandbox.serve_forever, daemon=True).start()
        self.addCleanup(self.sandbox.shutdown)
        self.addCleanup(self.sandbox.server_close)
        self.dummy = _dummy_process()
        self.addCleanup(_kill_later(self.dummy))
        self.launcher = LocalLauncher(make_config().data_dir)
        self.sid = "0" * 16
        self.launcher._write_pid(self.sid, self.dummy.pid, self.sandbox.port, "tok-123")

    def test_sandbox_is_found_after_restart(self):
        self.assertTrue(self.launcher.is_alive(self.sid))
        self.launcher.procs.clear()                       # 模拟控制面重启
        self.assertTrue(self.launcher.is_alive(self.sid))
        self.launcher.stop(self.sid)                      # 重启后也能停掉它
        self.assertFalse(self.launcher.is_alive(self.sid))
        # 先 SIGTERM，5 秒宽限期内没退出才升级 SIGKILL。慢机器/高负载下退出可能拖到宽限
        # 期之后，那时是 -9 而不是 -15：两种都是"被我们发的信号终止了"，断言只关心这点，
        # 不绑定具体是哪个信号（否则断言会随机红）。
        self.assertIn(_waited(self.dummy), (-15, -9))

    def test_wrong_token_means_not_ours(self):
        """pid 可能被系统复用给别的进程：令牌对不上就不是我们的沙箱，不能杀（#60）。"""
        self.launcher._write_pid(self.sid, self.dummy.pid, self.sandbox.port, "wrong-token")
        self.assertFalse(self.launcher.is_alive(self.sid))
        self.launcher.stop(self.sid)
        self.assertIsNone(self.dummy.poll())              # 无辜的进程还活着

    def test_reconcile_and_shutdown_cover_pid_files(self):
        other = "1" * 16
        dummy2 = _dummy_process()
        self.addCleanup(_kill_later(dummy2))
        self.launcher._write_pid(other, dummy2.pid, self.sandbox.port, "tok-123")
        self.launcher.reconcile({self.sid})               # other 不在 keep 里：停掉
        self.assertFalse(self.launcher.is_alive(other))
        self.assertIsNone(self.launcher._read_pid(other))      # pid 文件也收掉了
        _waited(dummy2)
        self.assertTrue(self.launcher.is_alive(self.sid))     # keep 里的不动
        self.launcher.shutdown()                              # 剩下的全停
        self.assertFalse(self.launcher.is_alive(self.sid))
        _waited(self.dummy)


if __name__ == "__main__":
    unittest.main()
