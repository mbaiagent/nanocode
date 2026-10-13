def split_bill(total_cents, people):
    """平均分摊账单，详见 SPEC.md。"""
    if people <= 0 or total_cents < 0:
        raise ValueError("people 必须是正整数，total_cents 不能是负数")
    base, extra = divmod(total_cents, people)
    return [base + 1 if i < extra else base for i in range(people)]
