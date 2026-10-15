"""沙箱启动器。make_launcher 按配置选择，auto 时选当前环境里隔离最强的一种。"""

from __future__ import annotations

from ..config import Config
from .base import SANDBOX_PORT, Launcher, LaunchError, SandboxSpec, wait_ready
from .docker import DockerClient, DockerLauncher
from .kubernetes import KubeClient, KubernetesLauncher, in_cluster
from .local import LocalLauncher


def make_launcher(cfg: Config, log=print) -> Launcher:
    choice = cfg.launcher
    if choice == "auto":
        docker = DockerClient()
        if in_cluster():
            choice = "k8s"
        elif docker.available() and docker.has_image(cfg.sandbox_image):
            choice = "docker"
        else:
            choice = "local"
            log(f"沙箱启动器：本机子进程（没有隔离）。要用容器隔离：构建镜像 {cfg.sandbox_image} 并确保 Docker 可用")
    if choice == "local":
        if "bypass" in cfg.sandbox_modes:
            raise ValueError("本机启动器没有隔离，NCS_SANDBOX_MODES 不能包含 bypass")
        return LocalLauncher(cfg.data_dir)
    if choice == "docker":
        return DockerLauncher(cfg.data_dir, cfg.sandbox_image, cfg.docker_network, cfg.docker_runtime,
                              cfg.sandbox_cpu, cfg.sandbox_memory)
    if choice == "k8s":
        return KubernetesLauncher(KubeClient.in_cluster(cfg.k8s_api), cfg.k8s_namespace, cfg.sandbox_image,
                                  cfg.k8s_runtime_class, cfg.k8s_storage_class, cfg.k8s_workspace_size,
                                  cfg.sandbox_cpu, cfg.sandbox_memory, cfg.token_ttl)
    raise ValueError(f"未知的沙箱启动器：{choice}（auto / local / docker / k8s）")


__all__ = ["make_launcher", "Launcher", "LaunchError", "SandboxSpec", "wait_ready", "SANDBOX_PORT",
           "LocalLauncher", "DockerLauncher", "KubernetesLauncher", "DockerClient", "KubeClient"]
