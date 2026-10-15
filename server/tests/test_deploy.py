"""部署清单和代码要一致：ConfigMap 里的沙箱策略必须等于控制面按同样配置生成的策略。"""

import json
import os
import re
import textwrap
import unittest

import support  # noqa: F401

from support import REPO, make_config

K8S = os.path.join(REPO, "deploy", "k8s")


def read(name):
    with open(os.path.join(K8S, name), encoding="utf-8") as f:
        return f.read()


def deployment_env(text: str) -> dict:
    return dict(re.findall(r"- \{name: (NCS_[A-Z_0-9]+), value: \"?([^\"}]+?)\"?\}", text))


class DeployConsistencyTest(unittest.TestCase):
    def test_policy_configmap_matches_control_plane_settings(self):
        cm = read("20-sandbox-policy.yaml")
        body = textwrap.dedent(cm.split("policy.json: |", 1)[1])
        policy = json.loads(body)
        env = deployment_env(read("50-control-plane.yaml"))
        cfg = make_config(gateway_url=env["NCS_GATEWAY_URL"], model=env["NCS_MODEL"],
                          sandbox_modes=env["NCS_SANDBOX_MODES"].split(","), sandbox_mode=env["NCS_SANDBOX_MODE"])
        self.assertEqual(policy, cfg.sandbox_policy("/data/home/audit.jsonl"))

    def test_gateway_and_control_plane_agree_on_model(self):
        self.assertEqual(deployment_env(read("50-control-plane.yaml"))["NCS_MODEL"],
                         deployment_env(read("60-gateway.yaml"))["NCS_MODEL"])

    def test_sandbox_labels_match_network_policy(self):
        from nanocode_server.launcher import KubeClient, KubernetesLauncher, SandboxSpec
        pod = KubernetesLauncher(make_config(), client=KubeClient("http://x")).pod_manifest(
            SandboxSpec("0" * 16, "a", "w" * 32, "g"))
        self.assertIn("matchLabels: {app: nanocode-sandbox}", read("30-network-policy.yaml"))
        self.assertEqual(pod["metadata"]["labels"]["app"], "nanocode-sandbox")
        self.assertIn("app: ncs-control", read("30-network-policy.yaml"))
        self.assertIn("labels: {app: ncs-control}", read("50-control-plane.yaml"))


if __name__ == "__main__":
    unittest.main()
