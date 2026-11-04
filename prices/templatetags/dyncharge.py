from decimal import ROUND_HALF_UP, Decimal

from django import template

register = template.Library()


@register.filter
def eu(value, decimals=2):
    """Format a number European style: 1.234,56 (like PHP number_format(x, 2, ',', '.')).

    Rounds half away from zero after trimming float noise, like PHP does,
    so 23.735 becomes 23,74 instead of 23,73.
    """
    if value is None:
        return ""
    decimals = int(decimals)
    d = Decimal(f"{float(value):.10f}").quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    s = f"{d:,.{decimals}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")
