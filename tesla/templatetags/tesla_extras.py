from django import template

register = template.Library()


@register.filter
def hhmm(minutes):
    """Minutes since midnight (as the car reports schedules) -> 'HH:MM'."""
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return ""
    return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"
