"""Serialize runtime instants unambiguously; stored naive timestamps represent UTC."""
from datetime import timezone


def utc_stamp(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
