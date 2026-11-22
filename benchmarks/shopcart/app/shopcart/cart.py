"""Shopping cart and checkout for the ShopCart demo store."""
from decimal import Decimal

from .pricing import bulk_discount, coupon_rate, gst, money, shipping_fee


class Product:
    def __init__(self, sku: str, name: str, price, stock: int = 100):
        self.sku = sku
        self.name = name
        self.price = Decimal(str(price))
        self.stock = stock


class Cart:
    def __init__(self):
        self.items = {}          # sku -> [product, quantity]
        self.coupon = None

    def add(self, product: Product, quantity: int = 1) -> None:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if product.sku in self.items:
            self.items[product.sku][1] += quantity
        else:
            self.items[product.sku] = [product, quantity]

    def set_quantity(self, sku: str, quantity: int) -> None:
        """Change the quantity of an item; a quantity of 0 removes the item."""
        if quantity < 0:
            raise ValueError("quantity cannot be negative")
        if quantity == 0:
            self.items.pop(sku, None)
            return
        self.items[sku][1] = quantity

    def remove(self, sku: str) -> bool:
        """Remove an item. Returns False if it was not in the cart."""
        return self.items.pop(sku, None) is not None

    def item_count(self) -> int:
        return sum(q for _, q in self.items.values())

    def subtotal(self) -> Decimal:
        return money(sum((p.price * q for p, q in self.items.values()), Decimal("0")))

    def apply_coupon(self, code: str) -> bool:
        if coupon_rate(code) > 0:
            self.coupon = code
            return True
        return False


def checkout(cart: Cart) -> dict:
    """Compute the final bill. Raises ValueError for an empty cart."""
    if not cart.items:
        raise ValueError("cart is empty")
    subtotal = cart.subtotal()
    discount = bulk_discount(subtotal)
    coupon = money((subtotal - discount) * coupon_rate(cart.coupon))
    taxable = subtotal - discount - coupon
    tax = gst(taxable)
    ship = shipping_fee(taxable)
    for product, qty in cart.items.values():
        if qty > product.stock:
            raise ValueError(f"not enough stock for {product.sku}")
    for product, qty in cart.items.values():
        product.stock -= qty
    return {
        "subtotal": subtotal, "discount": discount, "coupon": coupon, "tax": tax,
        "shipping": ship, "total": money(taxable + tax + ship),
        "average_item_price": money(subtotal / cart.item_count()),
    }
