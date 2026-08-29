"""Every user-facing string in NM Meet.

Spec section 10 is explicit: "Invalid selection" or "Booking failed" is not
acceptable — the user must be told what to do next, and every message names the
room and the time. Keeping the strings in one module is what makes that
reviewable, and what stops a message being invented at a call site.

Phase 1 needs only the actor messages below. The section 10 validation table and
the section 5 and 7 clash messages arrive with the booking endpoints in Phase 2.
"""

from __future__ import annotations

# --------------------------------------------------------------- actor / auth
# Used by app.core.auth. These are not from section 10 — the specification does
# not describe authentication — but they follow its rule: say what to do next.

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
