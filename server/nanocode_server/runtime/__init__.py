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
        elif cfg.auth != "dev" and not cfg.allow_unisolated:
            # 生产模式不许静默退化成没有隔离的本机子进程（#36）：运维以为有沙箱，
            # 其实每个用户的 Agent 都以控制面同一个系统用户运行，读得到主密钥和别人的工作区。
            # 宁可起不来，也不要带着"其实没有隔离"的假象运行。
            raise ValueError(
                f"NCS_LAUNCHER=auto 找不到可用的隔离沙箱（不在 K8s 集群里、Docker 不可用或没有镜像 "
                f"{cfg.sandbox_image}），生产模式拒绝退化成没有隔离的本机启动器。请构建沙箱镜像、"
                "启动 Docker，或显式设置 NCS_LAUNCHER=docker/k8s；确实要无隔离运行，"
                "请显式设置 NCS_ALLOW_UNISOLATED=1")
        else:
            choice = "local"
            log(f"沙箱启动器：本机子进程（没有隔离，只适合开发）。要用容器隔离：构建镜像 {cfg.sandbox_image} 并确保 Docker 可用")
    if choice == "local":
        if "bypass" in cfg.sandbox_modes:
            raise ValueError("本机启动器没有隔离，NCS_SANDBOX_MODES 不能包含 bypass")
        return LocalLauncher(cfg.data_dir)
    if choice == "docker":
        return DockerLauncher(cfg.data_dir, cfg.sandbox_image, cfg.docker_network, cfg.docker_runtime,
                              cfg.sandbox_cpu, cfg.sandbox_memory, max_lifetime=cfg.token_ttl)
    if choice == "k8s":
        return KubernetesLauncher(KubeClient.in_cluster(cfg.k8s_api), cfg.k8s_namespace, cfg.sandbox_image,
                                  cfg.k8s_runtime_class, cfg.k8s_storage_class, cfg.k8s_workspace_size,
                                  cfg.sandbox_cpu, cfg.sandbox_memory, cfg.token_ttl)
    raise ValueError(f"未知的沙箱启动器：{choice}（auto / local / docker / k8s）")


__all__ = ["make_launcher", "Launcher", "LaunchError", "SandboxSpec", "wait_ready", "SANDBOX_PORT",
           "LocalLauncher", "DockerLauncher", "KubernetesLauncher", "DockerClient", "KubeClient"]
