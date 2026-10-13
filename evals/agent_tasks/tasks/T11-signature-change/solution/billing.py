"""账单计算。金额一律用整数"分"。"""


def calc_total(items, *, tax_rate, discount=0):
    """items 是 [(单价, 数量), ...]；tax_rate 是税率，如 0.1 表示 10%；discount 是从小计里扣掉的金额（分）。
    返回含税总额（四舍五入到分）。"""
    subtotal = max(sum(price * qty for price, qty in items) - discount, 0)
    return round(subtotal * (1 + tax_rate))
