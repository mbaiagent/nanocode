import billing


class Cart:
    def __init__(self, tax=0.08):
        self.items = []
        self.tax = tax

    def add(self, price, qty=1):
        self.items.append((price, qty))

    def total(self):
        return billing.calc_total(self.items, self.tax)
