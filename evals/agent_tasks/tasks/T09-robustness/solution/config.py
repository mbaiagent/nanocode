DEFAULT_TIMEOUT = 30


def get_timeout(cfg):
    network = cfg.get("network") or {}
    if "timeout" not in network:
        return DEFAULT_TIMEOUT
    try:
        value = int(network["timeout"])
    except (TypeError, ValueError):
        raise ValueError(f"无效的 timeout：{network['timeout']!r}") from None
    if value <= 0:
        raise ValueError(f"timeout 必须大于 0：{value}")
    return value
