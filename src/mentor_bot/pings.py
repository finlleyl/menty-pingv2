from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def parse_iso_utc(s: str) -> datetime:
    """ISO-8601 → aware UTC; naive strings are treated as UTC."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def effective_last_contact(sheet_date, last_msg_iso, tz: ZoneInfo):
    candidates: list[datetime] = []
    if sheet_date is not None:
        candidates.append(
            datetime.combine(sheet_date, time(23, 59), tzinfo=tz).astimezone(timezone.utc)
        )
    if last_msg_iso:
        candidates.append(parse_iso_utc(last_msg_iso))
    return max(candidates) if candidates else None


def is_stopped(status, stop_list) -> bool:
    if not status:
        return False
    s = status.lower()
    return any(word in s for word in stop_list)


def in_send_window(now_local: datetime, start_hour: int, end_hour: int) -> bool:
    return start_hour <= now_local.hour < end_hour


def should_ping(*, last_contact, status, now_utc, stop_list, interval_days,
                unanswered, max_unanswered, paused_until_iso) -> bool:
    if last_contact is None:
        return False
    if is_stopped(status, stop_list):
        return False
    if unanswered >= max_unanswered:
        return False
    if paused_until_iso and parse_iso_utc(paused_until_iso) > now_utc:
        return False
    return now_utc - last_contact >= timedelta(days=interval_days)
