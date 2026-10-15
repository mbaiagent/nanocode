"""部署清单和代码要一致：清单里用到的 NCS_* 变量代码都认识，标签和网络策略对得上。"""

import os
import re
import unittest

import support  # noqa: F401

from support import REPO

K8S = os.path.join(REPO, "deploy", "k8s")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


KNOWN = set(re.findall(r'"(NCS_[A-Z0-9_]+)"', read(REPO, "server", "nanocode_server", "config.py")))


class DeployConsistencyTest(unittest.TestCase):
    def test_every_variable_in_manifests_is_known(self):
        for name in sorted(os.listdir(K8S)):
            used = set(re.findall(r"\b(NCS_[A-Z0-9_]+)", read(K8S, name)))
            with self.subTest(file=name):
                self.assertEqual(used - KNOWN, set(), "清单里有代码不认识的变量（拼错了？）")

    def test_secret_example_covers_what_services_need(self):
        secrets = set(re.findall(r"^\s+(NCS_[A-Z0-9_]+):", read(K8S, "40-secrets.example.yaml"), re.M))
        self.assertTrue({"NCS_SECRET_KEY", "NCS_PROXY_SECRET", "NCS_DATABASE_URL", "NCS_REDIS_URL"} <= secrets)

    def test_platform_model_settings_agree(self):
        def env(name):
            return dict(re.findall(r"\{name: (NCS_[A-Z_]+), value: \"?([^\"}]+?)\"?\}", read(K8S, name)))
        control, gateway = env("50-control-plane.yaml"), env("60-gateway.yaml")
        for key in ("NCS_PLATFORM_BASE_URL", "NCS_PLATFORM_MODELS"):
            self.assertEqual(control[key], gateway[key], key)

    def test_labels_match_network_policy(self):
        from nanocode_server.runtime import KubeClient, KubernetesLauncher, SandboxSpec
        pod = KubernetesLauncher(KubeClient("http://x"), "ns", "img").pod_manifest(SandboxSpec("0" * 16, "a", "w" * 32, "g"))
        policy = read(K8S, "30-network-policy.yaml")
        self.assertEqual(pod["metadata"]["labels"]["app"], "nanocode-sandbox")
        self.assertIn("matchLabels: {app: nanocode-sandbox}", policy)
        self.assertIn("labels: {app: ncs-control}", read(K8S, "50-control-plane.yaml"))
        self.assertIn("labels: {app: ncs-gateway}", read(K8S, "60-gateway.yaml"))

    def test_rbac_covers_what_the_launcher_creates(self):
        rbac = read(K8S, "50-control-plane.yaml")
        self.assertIn("resources: [pods, persistentvolumeclaims, configmaps]", rbac)


if __name__ == "__main__":
    unittest.main()
