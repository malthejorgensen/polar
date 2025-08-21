import uuid
from datetime import UTC, datetime


def utc_now() -> datetime:
    # Check if time travel is enabled for this request
    try:
        from polar.time_travel.middleware import get_time_with_travel_offset

        return get_time_with_travel_offset()
    except (ImportError, LookupError):
        # Fallback to real time if time travel module not available
        # or no context is set
        return datetime.now(UTC)


def generate_uuid() -> uuid.UUID:
    return uuid.uuid4()


def human_readable_size(num: float, suffix: str = "B") -> str:
    for unit in ("", "K", "M", "G", "T", "P", "E", "Z"):
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f} Y{suffix}"
