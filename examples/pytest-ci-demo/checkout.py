"""刻意保留折扣计算错误的演示代码；仅供隔离测试，不用于真实结算。"""


def discounted_total(subtotal, discount_percent):
    if subtotal < 0 or not 0 <= discount_percent <= 100:
        raise ValueError("invalid subtotal or discount")
    return round(subtotal * (1 - discount_percent), 2)
