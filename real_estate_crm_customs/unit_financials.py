"""Pure financial calculations for Real Estate Unit records.

This module intentionally has no Frappe dependency so the commercial formulas can
be regression-tested without a running site.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


RATE = Decimal("0.025")
ZERO = Decimal("0")


class UnitFinancialError(ValueError):
    """Raised when a Unit financial input is invalid."""


def _decimal(value, label):
    if value in (None, ""):
        return ZERO
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise UnitFinancialError(f"{label} must be a valid number.") from exc
    if result < ZERO:
        raise UnitFinancialError(f"{label} cannot be negative.")
    return result


def _round(value, precision):
    quantum = Decimal(1).scaleb(-int(precision))
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def calculate_unit_financials(paid=0, over_price=0, remaining=0, over_is_gross=False, precision=2):
    """Return server-authoritative Unit financial values.

    Total Price Net = Paid + Over Net + Remaining
    Property Tax = Paid * 2.5%
    Commission = Total Price Net * 2.5%
    Total Gross = Paid + Over Net + Property Tax + Commission + Remaining

    When Over Is Gross, the entered Over Price includes Over Net, Property Tax,
    and Commission. Algebraically this yields:
    Over Net = (Over Price - 5% * Paid - 2.5% * Remaining) / 1.025
    """

    paid_value = _decimal(paid, "Paid")
    over_value = _decimal(over_price, "Over Price")
    remaining_value = _decimal(remaining, "Remaining")

    if over_is_gross:
        over_net = (over_value - (Decimal("0.050") * paid_value) - (RATE * remaining_value)) / (
            Decimal("1.025")
        )
        if over_net < ZERO:
            raise UnitFinancialError(
                "Gross Over Price is too small to cover the calculated tax and commission."
            )
    else:
        over_net = over_value

    # Round stored components first, then derive stored totals from those values.
    # This guarantees the figures visible to agents reconcile exactly at currency
    # precision, including one-cent gross-over boundaries.
    paid_display = _round(paid_value, precision)
    over_price_display = _round(over_value, precision)
    over_net_display = _round(over_net, precision)
    remaining_display = _round(remaining_value, precision)
    property_tax = _round(paid_display * RATE, precision)
    total_price_net = paid_display + over_net_display + remaining_display
    commission = _round(total_price_net * RATE, precision)
    total_gross = paid_display + over_net_display + property_tax + commission + remaining_display
    down_payment = paid_display + over_price_display

    result = {
        "paid": paid_display,
        "over_price": over_price_display,
        "over_net": over_net_display,
        "down_payment": down_payment,
        "remaining": remaining_display,
        "property_tax": property_tax,
        "commission": commission,
        "total_price_net": total_price_net,
        "total_gross": total_gross,
    }

    return result
