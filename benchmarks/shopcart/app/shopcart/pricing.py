"""Pricing rules for the ShopCart demo store (amounts in rupees)."""
from decimal import ROUND_HALF_UP, Decimal

DISCOUNT_THRESHOLD = 1000      # orders of Rs 1,000 or more get a discount
DISCOUNT_RATE = Decimal("0.10")
FREE_SHIPPING_THRESHOLD = 500  # orders of Rs 500 or more ship free
SHIPPING_FEE = Decimal("50")
GST_RATE = Decimal("0.18")

COUPONS = {"SAVE10": Decimal("0.10"), "FESTIVE20": Decimal("0.20")}


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def bulk_discount(subtotal) -> Decimal:
    """10% discount for orders of Rs 1,000 or more."""
    subtotal = Decimal(str(subtotal))
    if subtotal >= DISCOUNT_THRESHOLD:
        return money(subtotal * DISCOUNT_RATE)
    return money(0)


def shipping_fee(amount) -> Decimal:
    """Flat Rs 50 shipping, free for orders of Rs 500 or more."""
    if Decimal(str(amount)) >= FREE_SHIPPING_THRESHOLD:
        return money(0)
    return money(SHIPPING_FEE)


def coupon_rate(code) -> Decimal:
    """Coupon codes are case-insensitive and ignore surrounding spaces."""
    if not code:
        return Decimal("0")
    return COUPONS.get(code.strip().upper(), Decimal("0"))


def gst(amount) -> Decimal:
    """18% GST rounded half-up to the paisa."""
    return money(Decimal(str(amount)) * GST_RATE)
