"""Delivery days named in a buyer's sentence, in English, Finnish and Swedish, relative to today in Helsinki.

    tomorrow / huomenna / huomiseksi / i morgon                 -> today + 1
    the day after tomorrow / ylihuomenna / i övermorgon         -> today + 2
    today / tänään / idag                                       -> today
    Thursday / torstaina / torstaiksi / on Thursday             -> the next Thursday after today
    next Monday / ensi maanantaina / this Friday / nästa måndag -> the next such day after today (not +7 more)
    next week Monday / ensi viikon maanantaina                  -> that weekday in the next calendar week
    in 3 days / 3 päivän päästä                                 -> today + 3

"next Monday" is read as the coming Monday (on a Saturday: in two days), which is what a kitchen usually means;
"ensi viikon maanantai" is the explicit next-week form. Absolute dates (2026-11-20, 20.11., 20 November) are
parsed by the callers before this. `deliverable` moves a day Valio doesn't deliver on (a Sunday or a Finnish public
holiday) to the next delivery day and says why, so the caller can declare it. `past_cutoff` says whether an order for that day is already past Valio's usual
order cut-off (17:00 Helsinki time the day before delivery, sous/deadline.py), so the agent can say so.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

WEEKDAY_WORDS = {
    0: ("monday", "maanantai", "måndag"), 1: ("tuesday", "tiistai", "tisdag"),
    2: ("wednesday", "keskiviikko", "onsdag"), 3: ("thursday", "torstai", "torsdag"),
    4: ("friday", "perjantai", "fredag"), 5: ("saturday", "lauantai", "lördag"), 6: ("sunday", "sunnuntai", "söndag"),
}
_DAY_AFTER = re.compile(r"\b(?:the\s+)?day\s+after\s+tomorrow\b|\bylihuom\w*|\bi\s+övermorgon\b", re.I)
_TOMORROW = re.compile(r"\btomorrow\b|\bhuomenna\b|\bhuomise\w*|\bhuomisaamu\w*|\bhuominen\b|\bi\s+morgon\b", re.I)
_TODAY = re.compile(r"\btoday\b|\btänään\b|\bidag\b", re.I)
_IN_DAYS = re.compile(r"\bin\s+(\d{1,2})\s+days?\b|"
                      r"\b(\d{1,2})\s+päivän\s+päästä\b|\bom\s+(\d{1,2})\s+dagar\b", re.I)
_NEXT_WEEK = re.compile(r"\bnext\s+week(?:'s)?\b|\bensi\s+viikon\b|\bnästa\s+veckas?\b", re.I)


def relative_date(text: str, today: date) -> date | None:
    """The delivery day a relative phrase in `text` names, or None."""
    t = " ".join(text.split())
    if _DAY_AFTER.search(t):
        return today + timedelta(days=2)
    if _TOMORROW.search(t):
        return today + timedelta(days=1)
    if m := _IN_DAYS.search(t):
        return today + timedelta(days=int(next(g for g in m.groups() if g)))
    for wd, words in WEEKDAY_WORDS.items():
        # English and Swedish as words; Finnish with its case endings (torstaina, torstaiksi, maanantaihin)
        if re.search(rf"\b(?:{words[0]}|{words[2]})(?:'s|s)?\b|\b{words[1]}\w*", t, re.I):
            if _NEXT_WEEK.search(t):
                monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
                return monday + timedelta(days=wd)
            return today + timedelta(days=(wd - today.weekday()) % 7 or 7)
    if _TODAY.search(t):
        return today
    return None


# --- days Valio Aimo does not deliver -------------------------------------------------------------------------
# Sundays and Finnish public holidays (plus Christmas Eve and Midsummer Eve, which wholesalers keep too). This is an
# assumption about the delivery calendar, not Valio data: a date moved off such a day is always declared to the
# gateway as a risk, so a human sees it.

def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l) // 433
    month = (h + l - 7 * m + 90) // 25
    return date(year, month, (h + l - 7 * m + 33 * month + 19) % 32)


def holidays(year: int) -> dict[date, str]:
    """Finnish public holidays (and the eves wholesalers keep) in `year`: {date: name}."""
    e = _easter(year)
    midsummer_eve = next(date(year, 6, d) for d in range(19, 26) if date(year, 6, d).weekday() == 4)
    all_saints = next(d for d in (date(year, 10, 31) + timedelta(days=i) for i in range(7)) if d.weekday() == 5)
    return {date(year, 1, 1): "New Year's Day", date(year, 1, 6): "Epiphany", e - timedelta(days=2): "Good Friday",
            e: "Easter Sunday", e + timedelta(days=1): "Easter Monday", date(year, 5, 1): "May Day",
            e + timedelta(days=39): "Ascension Day", midsummer_eve: "Midsummer Eve",
            midsummer_eve + timedelta(days=1): "Midsummer Day", all_saints: "All Saints' Day",
            date(year, 12, 6): "Independence Day", date(year, 12, 24): "Christmas Eve",
            date(year, 12, 25): "Christmas Day", date(year, 12, 26): "Boxing Day"}


def no_delivery_reason(day: date) -> str | None:
    """Why there is no delivery on `day` ('a Sunday', 'a public holiday (Christmas Day)'), or None."""
    if (h := holidays(day.year).get(day)) is not None:
        return f"a public holiday ({h})"
    if day.weekday() == 6:
        return "a Sunday"
    return None


def deliverable(day: date) -> tuple[date, str | None]:
    """The first delivery day on or after `day`, and why `day` itself isn't one (None if it is)."""
    why, d = no_delivery_reason(day), day
    while no_delivery_reason(d):
        d += timedelta(days=1)
    return d, why


def past_cutoff(day: date, now: datetime | None) -> bool:
    """Is `now` past Valio's usual order cut-off for delivery on `day` (17:00 Helsinki, the day before)?"""
    if now is None:
        return False
    from sous import deadline
    return now >= datetime.fromisoformat(deadline.estimate_cutoff(day)["at"])
