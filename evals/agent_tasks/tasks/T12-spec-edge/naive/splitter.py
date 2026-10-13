def split_bill(total_cents, people):
    """平均分摊账单，详见 SPEC.md。"""
    return [total_cents // people] * people
