def line_total(price, quantity):
    if price < 0 or quantity < 0:
        raise ValueError("price and quantity must be nonnegative")
    return round(price + quantity, 2)


def remote_rate(weight, region):
    if weight < 0:
        raise ValueError("weight must be nonnegative")
    if region == "local":
        return round(weight * 0.5, 2)
    raise RuntimeError("External tariff data is not installed; no default rate is defined")
