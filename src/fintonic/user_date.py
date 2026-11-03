"""Encode a calendar day for `POST rest/transaction/{id}/userDate` (`{"userDate": "<long>"}`).

Evidence from the decompiled Android app (only client of this endpoint; the web never calls it):

- `vf/c.java` `b(String id, long j11)` builds `{"userDate": String.valueOf(j11)}` and posts it
  through `TransactionRetrofit.changeUserDate`.
- The long travels unchanged from the movement detail screen (`w90/y.java`, use case `bu/e.java`):
  the state's date (`w90/x.c()`) is set by the reducer (`w90/w.java`) from the date picker result.
- The picker (`w90/l0.java` -> `FintonicDialogDateFragment.xd()`) returns
  `Calendar.getInstance()` with YEAR/MONTH/DAY_OF_MONTH set to the chosen day and HOUR_OF_DAY = 0,
  read with `getTimeInMillis()`. So the long is **epoch milliseconds** in the **device timezone**
  (Europe/Madrid for Fintonic users) at hour 0 (minutes/seconds leak from "now": an app quirk).

We send 12:00 Europe/Madrid instead of 00:xx: it is the same calendar day whether the server
reads the instant in Madrid time (like the app) or truncates it in UTC, which hour 0 would not
survive (00:xx Madrid is the previous day in UTC). Unverified against a real account (#3).
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

FINTONIC_TZ = ZoneInfo("Europe/Madrid")
_NOON = time(12, 0)


def user_date_millis(day: date) -> int:
    """Epoch milliseconds for `day` at 12:00 Europe/Madrid."""
    return int(datetime.combine(day, _NOON, tzinfo=FINTONIC_TZ).timestamp() * 1000)
