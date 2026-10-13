from billing import calc_total as total


def monthly_summary(orders):
    """orders: {月份: [(单价, 数量), ...]} -> {月份: 含税总额}，税率固定 5%。"""
    return {month: total(rows, tax_rate=0.05) for month, rows in orders.items()}
