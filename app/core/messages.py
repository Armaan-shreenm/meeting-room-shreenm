"""Every user-facing string in NM Meet.

Spec section 10 is explicit: "Invalid selection" or "Booking failed" is not
acceptable - the user must be told what to do next, and every message names the
room and the time. Keeping the strings in one module is what makes that
reviewable, and what stops a message being invented at a call site.

The SECTION 10 block below reproduces the specification's table verbatim. Do not
reword those strings; they are the contract. Strings outside that block cover
situations the table does not list, and follow its rule: say what to do next.

Times inside messages are written the way the specification writes them - 
"1 pm", "3 pm", "1:30 pm" - never "13:00". Use
:func:`app.core.time.format_clock` for every one of them.
"""

from __future__ import annotations

# =============================================================================
# SECTION 10 - VALIDATION AND MESSAGES, verbatim from the specification
# =============================================================================

# Exit time is before or equal to entry time
EXIT_NOT_AFTER_ENTRY = "The meeting has to end after it starts."

# Shorter than 30 minutes
TOO_SHORT = "The shortest booking is 30 minutes."

# Longer than 4 hours
TOO_LONG = "Four hours is the longest single booking. Split it into two."

# Outside office hours. The specification wrote "between 9 am and 8 pm"; the
# hours are now configurable, so the sentence is built from them rather than
# going stale the moment somebody changes OPEN_TIME.
OUTSIDE_HOURS_TEMPLATE = "Rooms can be booked between {open} and {close}."

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
# Beyond section 10 - situations the table does not list
# =============================================================================

# Spec section 4: "Minutes snap to 30. Only :00 and :30 can be selected."
# Test T-15 requires this be impossible; the table gives no wording for it.
NOT_ON_SLOT_BOUNDARY = "Meetings start and end on the hour or the half hour."

# Spec field 2: "Today to today + 90 days."
TOO_FAR_AHEAD = "You can book today and the next {days} days only."

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
BAD_TIME = "That is not a time NM Meet understands. Pick one of the times offered."

# Advisory only, shown against a room on the picker. Never blocks a booking:
# nobody verifies the headcount, so refusing on it would be theatre.
ROOM_MIN_PEOPLE = "{room} should only be selected if you are {count} or more people."

# Rate limiting. Section 10's rule applies: say what to do next.
TOO_MANY_REQUESTS = (
    "That is a lot of bookings at once. Wait a moment and try again."
)

# The catch-all. A stack trace never reaches a user; the request id does, so
# somebody can find the real error in the logs.
UNEXPECTED_ERROR = (
    "Something went wrong at our end and the booking was not changed. "
    "Try again, and quote reference {request_id} if it keeps happening."
)


# =============================================================================
# Permissions and lifecycle
# =============================================================================
# There are no privilege levels. Whoever booked a room is the only person who
# can cancel or change it - not reception, not an administrator, not an
# attendee. An attendee may only accept or decline their own place.

CANNOT_CANCEL = "Only {owner} booked this room, so only {owner} can cancel it."

CANNOT_EDIT = "Only {owner} booked this room, so only {owner} can change it."

ALREADY_CANCELLED = "That meeting was already cancelled."

CANNOT_CHANGE_CANCELLED = (
    "That meeting was cancelled and can no longer be changed. Book it again "
    "instead."
)

# Room, date and time are never editable - changing them is a cancel and re-book.
IMMUTABLE_FIELDS = (
    "The room, the date and the time cannot be changed. Cancel this booking and "
    "make a new one."
)

# An attendee responding to their own invitation.
NOT_AN_ATTENDEE = "You are not on the attendee list for this meeting."

BAD_RESPONSE = "Answer with ACCEPTED or DECLINED."


# =============================================================================
# Actor / authentication
# =============================================================================
# The specification does not describe authentication. These follow its rule.

ACTOR_NOT_IDENTIFIED = "Please sign in to NM Meet to book or change a room."

BAD_CREDENTIALS = (
    "That email and password do not match an NM Meet account. Check them and "
    "try again, or ask an administrator to reset your password."
)

NO_PASSWORD_SET = (
    "No password has been set for {email}. Ask an administrator to set one."
)

CSRF_FAILED = (
    "Your session could not be verified. Reload the page and try again."
)

SIGNED_OUT = "You have been signed out."


# =============================================================================
# Google sign-in
# =============================================================================

GOOGLE_NOT_CONFIGURED = (
    "Google sign-in is not set up yet. Ask IT to add the Google client details."
)

GOOGLE_WRONG_DOMAIN = (
    "{email} is not a {domain} address. Sign in with your work Google account."
)

GOOGLE_FAILED = "Google could not confirm who you are. Try signing in again."

GOOGLE_STATE_MISMATCH = (
    "That sign-in link has expired. Start again from the sign-in page."
)

GOOGLE_EMAIL_UNVERIFIED = (
    "Google has not verified {email}. Verify it with Google and try again."
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


def outside_hours() -> str:
    """Section 10's wording, built from the configured office hours."""
    # Imported here: config must not import messages.
    from app.config import settings
    from app.core.time import format_clock

    return OUTSIDE_HOURS_TEMPLATE.format(
        open=format_clock(settings.open_minutes),
        close=format_clock(settings.close_minutes),
    )


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
