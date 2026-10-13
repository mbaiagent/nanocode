import functools

import billing

# 导出给财务系统的总额，税率固定 8%
export_total = functools.partial(billing.calc_total, tax_rate=0.08)
