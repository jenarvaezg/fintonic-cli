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

Verified against a real account (#3): the server turns the instant into a day by rounding **up**
to the next UTC midnight (00:00Z → that day; 23:00Z the day before → that day; 11:00Z → the next
day). Midnight Europe/Madrid, like the app, always lands 1-2 h before the UTC day boundary, so it
maps to the intended day in winter and summer time.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

FINTONIC_TZ = ZoneInfo("Europe/Madrid")
_MIDNIGHT = time(0, 0)


def user_date_millis(day: date) -> int:
    """Epoch milliseconds for `day` at 00:00 Europe/Madrid (what the Android app sends)."""
    return int(datetime.combine(day, _MIDNIGHT, tzinfo=FINTONIC_TZ).timestamp() * 1000)
