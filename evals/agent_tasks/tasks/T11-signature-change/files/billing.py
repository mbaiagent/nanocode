"""账单计算。金额一律用整数"分"。"""


def calc_total(items, tax):
    """items 是 [(单价, 数量), ...]；tax 是税率，如 0.1 表示 10%。返回含税总额（四舍五入到分）。"""
    subtotal = sum(price * qty for price, qty in items)
    return round(subtotal * (1 + tax))
