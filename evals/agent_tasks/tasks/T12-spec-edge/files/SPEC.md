# split_bill(total_cents, people) -> list[int]

把一笔账单平均分给若干人，金额都用整数"分"。

- 返回长度为 `people` 的列表，第 i 个元素是第 i 个人应付的金额。
- 所有人应付金额之和必须正好等于 `total_cents`，一分都不能多、不能少。
- 不能整除时，多出来的零头每人 1 分，从第一个人开始依次分配（所以靠前的人可能比靠后的多付 1 分）。
- `people` 必须是正整数，`total_cents` 不能是负数，否则抛出 `ValueError`。
