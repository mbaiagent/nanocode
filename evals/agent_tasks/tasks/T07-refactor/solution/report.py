def _format_row(label, amount):
    return f"{label.strip().title():<12}{f'{amount:,.2f}':>12}"


def sales_line(name, amount):
    return _format_row(name, amount)


def refund_line(name, amount):
    return _format_row(name, -abs(amount))


def total_line(amount):
    return _format_row("total", amount)
