"""Every user-facing string in NM Meet.

Spec section 10 is explicit: "Invalid selection" or "Booking failed" is not
acceptable — the user must be told what to do next, and every message names the
room and the time. Keeping the strings in one module is what makes that
reviewable, and what stops a message being invented at a call site.

The SECTION 10 block below reproduces the specification's table verbatim. Do not
reword those strings; they are the contract. Strings outside that block cover
situations the table does not list, and follow its rule: say what to do next.

Times inside messages are written the way the specification writes them —
"1 pm", "3 pm", "1:30 pm" — never "13:00". Use
:func:`app.core.time.format_clock` for every one of them.
"""

from __future__ import annotations

# =============================================================================
# SECTION 10 — VALIDATION AND MESSAGES, verbatim from the specification
# =============================================================================

# Exit time is before or equal to entry time
EXIT_NOT_AFTER_ENTRY = "The meeting has to end after it starts."

# Shorter than 30 minutes
TOO_SHORT = "The shortest booking is 30 minutes."

# Longer than 4 hours
TOO_LONG = "Four hours is the longest single booking. Split it into two."

# Outside 09:00-20:00
OUTSIDE_HOURS = "Rooms can be booked between 9 am and 8 pm."

# Date is in the past
DATE_PAST = "That date has passed."

# Date is a Sunday or a holiday
OFFICE_CLOSED = "The office is closed that day."

# Taken while the form was open
# e.g. "Power was booked by someone else a moment ago. Pulse and Switch are
#       still free from 1 pm to 3 pm."
ROOM_TAKEN = (
    "{room} was booked by someone else a moment ago. "
    "{free_rooms} still free from {entry} to {exit}."
)

# No room free for the chosen window
# e.g. "All five rooms are booked between 1 pm and 3 pm. The first free room is
#       Pulse at 3 pm."
NO_ROOM_FREE = (
    "All {room_count} rooms are booked between {entry} and {exit}. "
    "The first free room is {first_room} at {first_time}."
)

# No room free for the chosen window, and none frees up later that day either.
# Section 10 gives no wording for this; it follows the rule above.
NO_ROOM_FREE_ALL_DAY = (
    "All {room_count} rooms are booked between {entry} and {exit}, "
    "and nothing frees up later that day. Try another date."
)

# Department not chosen
DEPARTMENT_REQUIRED = "Please choose a department."

# Nobody named as conducting
CONDUCTOR_REQUIRED = "Please say who is conducting the meeting."


# =============================================================================
# Beyond section 10 — situations the table does not list
# =============================================================================

# Spec section 4: "Minutes snap to 30. Only :00 and :30 can be selected."
# Test T-15 requires this be impossible; the table gives no wording for it.
NOT_ON_SLOT_BOUNDARY = "Meetings start and end on the hour or the half hour."

# Spec field 2: "Today to today + 90 days."
TOO_FAR_AHEAD = "You can book up to {days} days ahead."

# The chosen entry time is inside a window the room is already booked for.
ENTRY_ALREADY_BOOKED = "{room} is already booked at {entry}. Pick another time."

# Today, for a time that has already gone.
TIME_ALREADY_PASSED = "That time has already passed today."

UNKNOWN_ROOM = "There is no room called {room}."
UNKNOWN_DEPARTMENT = "That department is not on the list. Please choose one."
UNKNOWN_BOOKING = "That booking no longer exists. It may have been cancelled."

# Field 6: "Conducted by" must be a directory member (decision D-04).
CONDUCTOR_NOT_IN_DIRECTORY = (
    "{name} is not in the company directory and cannot conduct a meeting."
)
ATTENDEE_NOT_IN_DIRECTORY = (
    "{name} is not in the company directory and cannot be added as an attendee."
)

BAD_DATE = "That is not a date NM Meet understands. Use the date picker."
BAD_TIME = "That is not a time NM Meet understands. Use the clock picker."


# =============================================================================
# Actor / authentication
# =============================================================================
# The specification does not describe authentication. These follow its rule.

ACTOR_NOT_IDENTIFIED = (
    "You are not signed in. Send your directory email in the X-User-Email header."
)

ACTOR_UNKNOWN = (
    "{email} is not in the company directory. Ask reception or an administrator "
    "to add you before booking a room."
)

ACTOR_DEACTIVATED = (
    "The account for {email} is deactivated. Ask reception or an administrator "
    "to reactivate it."
)


# =============================================================================
# Helpers for assembling the two clash messages
# =============================================================================

# Spec section 10 writes "All five rooms are booked...". The count is spelled out
# for the sizes a branch will realistically have; anything larger uses digits.
_NUMBER_WORDS = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}


def spell_count(count: int) -> str:
    """Render a small count as a word, as section 10 does with "five"."""
    return _NUMBER_WORDS.get(count, str(count))


def join_names(names: list[str]) -> str:
    """Join room names the way the specification does.

    "Pulse", "Pulse and Switch", "Pulse, Ignite and Switch".
    """
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def room_taken(
    room: str, free_rooms: list[str], entry: str, exit_: str
) -> str:
    """Build the section 5 / section 7 clash message.

    ``free_rooms`` is computed from live availability, never hardcoded.
    """
    joined = join_names(free_rooms)
    # "Pulse is still free" for one room, "Pulse and Switch are still free" for more.
    verb = "is" if len(free_rooms) == 1 else "are"
    return ROOM_TAKEN.format(
        room=room,
        free_rooms=f"{joined} {verb}",
        entry=entry,
        exit=exit_,
    )


def no_room_free(
    room_count: int,
    entry: str,
    exit_: str,
    first_room: str | None,
    first_time: str | None,
) -> str:
    """Build the section 10 "no room free" message.

    ``first_room`` and ``first_time`` are computed by walking the day's
    availability. When nothing frees up at all, the wording changes rather than
    naming a room that does not exist.
    """
    if first_room is None or first_time is None:
        return NO_ROOM_FREE_ALL_DAY.format(
            room_count=spell_count(room_count), entry=entry, exit=exit_
        )
    return NO_ROOM_FREE.format(
        room_count=spell_count(room_count),
        entry=entry,
        exit=exit_,
        first_room=first_room,
        first_time=first_time,
    )
