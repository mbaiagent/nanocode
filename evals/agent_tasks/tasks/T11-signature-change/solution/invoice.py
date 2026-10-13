from billing import calc_total

TAX = 0.1


def invoice_total(lines):
    """lines: [{"price": 单价, "qty": 数量}, ...]"""
    return calc_total([(l["price"], l["qty"]) for l in lines], tax_rate=TAX)
