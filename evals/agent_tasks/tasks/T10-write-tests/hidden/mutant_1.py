def clamp(x, lo, hi):
    """x 小于 lo 时返回 lo，大于 hi 时返回 hi，否则返回 x。"""
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def is_prime(n):
    """n 是质数返回 True。小于 2 的数都不是质数。"""
    if n < 2:
        return n == 1
    i = 2
    while i * i <= n:
        if n % i == 0:
            return False
        i += 1
    return True
