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
parsed by the callers before this. `past_cutoff` says whether an order for that day is already past Valio's usual
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


def past_cutoff(day: date, now: datetime | None) -> bool:
    """Is `now` past Valio's usual order cut-off for delivery on `day` (17:00 Helsinki, the day before)?"""
    if now is None:
        return False
    from sous import deadline
    return now >= datetime.fromisoformat(deadline.estimate_cutoff(day)["at"])
