"""#123：沙箱只能连宿主机上的网关端口——固定网桥名 / 网段、网络校验、防火墙脚本。"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import support  # noqa: F401

from nanocode_server.config import Config
from nanocode_server.runtime import DockerLauncher, LaunchError
from support import make_config

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIREWALL = os.path.join(ROOT, "deploy", "firewall")
BASH = shutil.which("bash") if os.name != "nt" else None
BRIDGE_OPT = "com.docker.network.bridge.name"
ICC = "com.docker.network.bridge.enable_icc"


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class FakeDocker:
    def __init__(self, net=None):
        self.net, self.created = net, None

    def request(self, method, path, body=None, timeout=60):
        if method == "GET" and path.startswith("/networks/"):
            return (200, self.net) if self.net is not None else (404, {})
        if method == "POST" and path == "/networks/create":
            self.created = body
            subnet = ((body.get("IPAM") or {}).get("Config") or [{}])[0].get("Subnet", "172.19.0.0/16")
            self.net = {"Driver": "bridge", "Internal": body["Internal"], "Options": dict(body["Options"]),
                        "IPAM": {"Config": [{"Subnet": subnet, "Gateway": subnet.rsplit(".", 1)[0] + ".1"}]}}
            return 201, {}
        return 200, {}


def launcher(net=None, bridge="ncs-sbx0", subnet="172.30.250.0/24"):
    d = DockerLauncher.__new__(DockerLauncher)
    d.network, d.docker, d.bridge_name, d.subnet = "ncs-sandboxes", FakeDocker(net), bridge, subnet
    return d


class FixedNetworkTest(unittest.TestCase):
    GOOD = {"Driver": "bridge", "Internal": True, "Options": {ICC: "false", BRIDGE_OPT: "ncs-sbx0"},
            "IPAM": {"Config": [{"Subnet": "172.30.250.0/24", "Gateway": "172.30.250.1"}]}}

    def test_created_network_has_fixed_bridge_and_subnet(self):
        d = launcher()
        self.assertEqual(d._ensure_network(), "172.30.250.1")
        self.assertEqual(d.docker.created["Options"][BRIDGE_OPT], "ncs-sbx0")
        self.assertEqual(d.docker.created["IPAM"], {"Config": [{"Subnet": "172.30.250.0/24"}]})
        self.assertTrue(d.docker.created["Internal"])

    def test_existing_matching_network_is_accepted(self):
        self.assertEqual(launcher(self.GOOD)._ensure_network(), "172.30.250.1")

    def test_mismatched_network_is_refused(self):
        """防火墙规则按网桥名 / 网段写：对不上就等于没配，必须拒绝启动。"""
        wrong_bridge = {**self.GOOD, "Options": {ICC: "false", BRIDGE_OPT: "br-123"}}
        no_bridge = {**self.GOOD, "Options": {ICC: "false"}}
        wrong_subnet = {**self.GOOD, "IPAM": {"Config": [{"Subnet": "172.18.0.0/16", "Gateway": "172.18.0.1"}]}}
        for net in (wrong_bridge, no_bridge, wrong_subnet):
            with self.subTest(net=net):
                with self.assertRaises(LaunchError):
                    launcher(net)._ensure_network()

    def test_create_failure_carries_docker_reason(self):
        d = launcher()
        d.docker.request = lambda method, path, body=None, timeout=60: (
            (404, {}) if method == "GET" else (400, {"message": "Pool overlaps with other one on this address space"}))
        with self.assertRaisesRegex(LaunchError, "Pool overlaps.*NCS_DOCKER_SUBNET"):
            d._ensure_network()

    def test_unpinned_launcher_keeps_old_behaviour(self):
        d = launcher(bridge="", subnet="")
        d._ensure_network()
        self.assertNotIn(BRIDGE_OPT, d.docker.created["Options"])
        self.assertNotIn("IPAM", d.docker.created)

    def test_config_defaults_and_validation(self):
        self.assertEqual((Config().docker_bridge, Config().docker_subnet), ("ncs-sbx0", "172.30.250.0/24"))
        make_config(docker_bridge="", docker_subnet="").validate()
        for kw in ({"docker_bridge": "a-very-long-bridge-name"}, {"docker_bridge": "bad name"},
                   {"docker_subnet": "172.30.250.1/24"}, {"docker_subnet": "nope"},
                   {"docker_subnet": "fd00::/64"}, {"docker_subnet": "10.0.0.0/30"}):
            with self.subTest(kw=kw):
                with self.assertRaises(ValueError):
                    make_config(**kw).validate()


FAKE_FIREWALL_CMD = r"""#!/usr/bin/env bash
# 假 firewall-cmd：记录调用，模拟一个已有 zone（里面有一个多余端口），网段原来在 trusted 里
echo "$*" >> "$FW_LOG"
case "$*" in
  "--state") exit 0 ;;
  "--permanent --get-zones") echo "block docker drop public trusted" ;;
  "--permanent --get-zone-of-source="*) echo trusted ;;
  *"--list-services") echo "ssh" ;;
  *"--list-ports") echo "9999/tcp" ;;
  *"--list-sources") echo "10.9.9.0/24" ;;
  *"--list-rich-rules") printf '%s\n' 'rule family="ipv4" port port="22" protocol="tcp" accept' ;;
  *"--query-"*) exit 1 ;;
esac
exit 0
"""


@unittest.skipUnless(BASH, "需要 bash（Windows 上不跑部署脚本）")
class FirewallScriptTest(unittest.TestCase):
    def test_scripts_parse(self):
        for name in os.listdir(FIREWALL):
            if name.endswith(".sh"):
                with self.subTest(script=name):
                    subprocess.run([BASH, "-n", os.path.join(FIREWALL, name)], check=True)

    def run_firewalld(self, **env):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        fake, log = os.path.join(tmp, "firewall-cmd"), os.path.join(tmp, "log")
        with open(fake, "w") as f:
            f.write(FAKE_FIREWALL_CMD)
        os.chmod(fake, 0o755)
        r = subprocess.run([BASH, os.path.join(FIREWALL, "ncs-sandbox-firewalld.sh")], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env={**os.environ, "FIREWALL_CMD": fake, "FW_LOG": log, **env})
        calls = read(log).splitlines() if os.path.exists(log) else []
        return r, calls

    def test_firewalld_zone_drops_everything_but_gateway(self):
        r, calls = self.run_firewalld(NCS_DOCKER_SUBNET="172.30.250.0/24", NCS_GATEWAY_PORT="8701")
        self.assertEqual(r.returncode, 0, r.stderr)
        z = "--permanent --zone=ncs-sandbox"
        for want in ("--permanent --new-zone=ncs-sandbox",
                     "--permanent --zone=trusted --remove-source=172.30.250.0/24",   # 网段只能在一个 zone 里
                     f"{z} --set-target=DROP",
                     f"{z} --remove-service=ssh", f"{z} --remove-port=9999/tcp", f"{z} --remove-source=10.9.9.0/24",
                     f'{z} --remove-rich-rule=rule family="ipv4" port port="22" protocol="tcp" accept',
                     f"{z} --add-source=172.30.250.0/24", f"{z} --add-port=8701/tcp", "--reload"):
            self.assertIn(want, calls)
        self.assertLess(calls.index(f"{z} --set-target=DROP"), calls.index("--reload"))

    def test_firewalld_rejects_bad_input(self):
        for env in ({"NCS_GATEWAY_PORT": "8701;rm"}, {"NCS_DOCKER_SUBNET": "any"}, {"NCS_FIREWALL_ZONE": "a b"}):
            with self.subTest(env=env):
                r, calls = self.run_firewalld(**env)
                self.assertEqual(r.returncode, 2)
                self.assertEqual(calls, [])

    def test_nftables_rules(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        fake = os.path.join(tmp, "nft")
        with open(fake, "w") as f:
            f.write("#!/usr/bin/env bash\nexit 0\n")
        os.chmod(fake, 0o755)
        out = os.path.join(tmp, "ncs.nft")
        r = subprocess.run([BASH, os.path.join(FIREWALL, "ncs-sandbox-nftables.sh")], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env={**os.environ, "NFT_CMD": fake, "NFT_FILE": out, "NCS_GATEWAY_PORT": "8701"})
        self.assertEqual(r.returncode, 0, r.stderr)
        rules = read(out)
        self.assertIn('iifname "ncs-sbx0" tcp dport 8701 accept', rules)
        self.assertIn('iifname "ncs-sbx0" counter drop', rules)
        self.assertIn("ip saddr 172.30.250.0/24 counter drop", rules)
        self.assertIn("ct state established,related accept", rules)
        # accept 必须在 drop 前面
        self.assertLess(rules.index("tcp dport 8701 accept"), rules.index('iifname "ncs-sbx0" counter drop'))


if __name__ == "__main__":
    unittest.main()
