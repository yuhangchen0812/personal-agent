"""刻意保留满额免运费的边界错误。"""


def shipping_fee(subtotal):
    if subtotal < 0:
        raise ValueError("subtotal must be nonnegative")
    if subtotal > 100:
        return 0
    return 5
