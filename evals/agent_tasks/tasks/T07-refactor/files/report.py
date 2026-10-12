def sales_line(name, amount):
    label = name.strip().title()
    value = f"{amount:,.2f}"
    return f"{label:<12}{value:>12}"


def refund_line(name, amount):
    label = name.strip().title()
    value = f"{-abs(amount):,.2f}"
    return f"{label:<12}{value:>12}"


def total_line(amount):
    label = "total".strip().title()
    value = f"{amount:,.2f}"
    return f"{label:<12}{value:>12}"
