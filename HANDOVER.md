# NM Meet - handover

Everything a new maintainer needs that is **not** obvious from the code: which
decisions were taken and why, what the documentation still does not answer, and
what is deliberately not built.

Built to `NM_Meet_Booking_Documentation.pdf` v1.0 (26 August 2026). That document
is the contract. The approved frontend is `nm-meet-web_1.html`; its design is
final and `static/index.html` is that file with only its data layer replaced.

---

## 1. Section 13 - the open decisions, and what was implemented

| # | Question | Recommendation | What was built |
| --- | --- | --- | --- |
| **D-01** | Every booking to the Mumbai group, or a daily summary? | Daily 8 am summary | **Both halves honoured.** A `notification_log` row is written for the branch group on *every* event and left `QUEUED`; `scripts/daily_summary.py` delivers one summary and marks them `SENT`. Attendees, conductor, reception and booker are sent immediately. This is why T-09 still counts five notifications. |
| **D-02** | Calendar invite now that Meet is dropped? | Yes, invite; no video link | **Partly built.** `CALENDAR_INVITE_ENABLED` exists and no Google Meet link is ever generated (asserted in tests). The `.ics` attachment itself is **not implemented** - see §5. |
| **D-03** | Is Saturday a working day? Is Sunday always closed? | Mon-Sat, Sunday closed | Built. `CLOSED_WEEKDAYS=[6]`, computed, never stored as holiday rows. Sunday returns `closed_reason: "weekly_closure"` and the grid greys the day. |
| **D-04** | Can the conductor be outside the company? | No - directory only | Built. `conducted_by` and every attendee must be an **active** directory user; a deactivated account is refused by name. |
| **D-05** | Gap needed between meetings? | No - back to back allowed | Built, and enforced by the database. The `'[)'` half-open range in `no_double_booking` is what makes 15:00→15:00 legal. T-03 and T-04. |
| **D-06** | How long before an unused room is released? | 15 minutes, by reception | Built. `POST /api/bookings/{id}/no-show`, reception or admin only, refused earlier than `entry_time + 15 min`, sets `NO_SHOW`, audit-logged, **no notification**. Reversible via `/restore`, adjudicated by the exclusion constraint. |
| **D-07** | 30-minute or 15-minute granularity? | 30 minutes | Built. `SLOT_MINUTES=30`; a `:15` entry is refused server-side even from a hand-written request (T-15). |

## 2. Decisions settled during the build

These were raised as questions and answered by the business; they are spec now.

| Area | Decision |
| --- | --- |
| Ids | `bookings`, `booking_attendees`, `notification_log`, `audit_log` use application-generated **UUID4**. Rooms keep their slug; departments and users keep integer ids - neither is ever exposed in a link. |
| Attendee reply | `attendee_response` enum: `PENDING` \| `ACCEPTED` \| `DECLINED`. Record-keeping only: no notification fires and nothing changes on the grid. |
| Notification delivery | `notification_status` enum: `QUEUED` \| `SENT` \| `FAILED`. The row is written `QUEUED` **before** the transport is touched, so a crash mid-send leaves evidence. |
| Editing | Details only - title, department, conducted_by, attendees, reception note. **Room, date and time are never editable**; changing them is a cancel and a re-book, so the exclusion constraint re-adjudicates. |
| Who may edit | Exactly the cancel list: booker, conductor, reception, admin. An attendee may only change their own `response_status`. |
| Attendee churn | Newly added get `BOOKED`, removed get `CANCELLED`, everyone else `CHANGED`. |
| Released slots | A `CANCELLED` or `NO_SHOW` window is bookable by anyone, including the original host. |
| Directory | Admin-maintained and seeded. No HR sync. |
| Holidays | Reception and admin maintain them. Never seeded - `scripts/load_holidays.py` imports a CSV. |
| Mailboxes | `reception.mumbai@shreenm.com`, `mumbai.all@shreenm.com`. |

## 3. Things the code does that the documentation does not say

Each of these was a gap. The conservative option was taken and is recorded here.

1. **Section 10 has no wording for a `:15` entry or for exceeding 90 days.**
   Added `NOT_ON_SLOT_BOUNDARY` ("Meetings start and end on the hour or the half
   hour.") and `TOO_FAR_AHEAD`, written in the table's style.
2. **An overlap of any kind is one thing.** An exit that runs into the next
   booking is a clash, answered with section 5's wording. Refusing it separately
   would invent a message the section 10 table does not contain.
3. **"All five rooms" spells the count.** `spell_count()` renders 1-10 as words,
   larger numbers as digits, so the message survives a sixth room being added.
4. **Authentication is not in the specification at all.** Email plus bcrypt
   password, signed session cookie, CSRF double-submit. Login messages follow
   section 10's rule: say what to do next, and never reveal who has an account.
5. **`booking_date` cannot be enforced by a CHECK constraint**, because
   PostgreSQL refuses `AT TIME ZONE` in one (it is STABLE, not IMMUTABLE). It is
   enforced by a `before_flush` hook instead, and covered by tests including the
   19:00 UTC case that is already tomorrow in Mumbai.
6. **`iso()` in the approved prototype was wrong.** It formatted via
   `toISOString()`, i.e. UTC, so a `Date` at local midnight came out as the
   previous day in Asia/Kolkata. Harmless while the data was hardcoded and
   self-consistent; wrong the moment real dates arrived. Fixed to format
   locally. The original file is genuinely unusable in IST after midday - its
   "Tomorrow" chip resolves to today and disables every morning hour.
7. **Closed days are greyed on the grid** using the prototype's own `.gone`
   class, already used for past times. No new CSS. Without it a user walks into
   a wizard the server will certainly refuse.

## 4. Questions the documentation still does not answer

Nobody has decided these. They are not blocking, and the conservative behaviour
is described.

1. **Password reset.** There is no self-service reset and no "forgot password"
   flow. An administrator must set a password directly. The login screen says so.
2. **Who administers the directory, in the product?** There is no admin screen.
   Adding a user today means a database write or editing `scripts/seed.py`.
3. **Should a booking be editable after it has ended?** Currently yes - only
   `CANCELLED` blocks an edit. Arguably a past meeting should be frozen.
4. **How long is history kept?** Nothing is ever deleted. `audit_log` and
   `notification_log` grow without bound. No retention policy was specified.
5. **What happens to bookings when a user is deactivated?** They remain, and the
   person stays named as booker or conductor. Nothing reassigns them.
6. **Is the reception note visible to attendees?** It is returned to anyone who
   can see the booking. The specification calls it "a note for reception" and
   says it appears on the front desk sheet, which may imply it should not be.
7. **Should the branch summary list cancellations**, or only what is booked?
   Currently it lists the day's confirmed meetings only.
8. **Time zone of a user travelling.** Everything is Asia/Kolkata. A user in
   another zone sees Mumbai time with no indication of that.
9. **Two directory members with the same full name** would collide in the
   frontend's name→id lookup on the details step. The API is id-based
   throughout; only that display shortcut is affected.

## 5. Deliberately not built

| | Why |
| --- | --- |
| **`.ics` calendar attachment** (D-02) | The decision is recorded and `CALENDAR_INVITE_ENABLED` exists, but no invite is generated. Attaching one correctly means `METHOD:REQUEST`, `SEQUENCE` handling on edits and `METHOD:CANCEL` on cancellation - a phase of its own, and getting it half right puts wrong meetings in people's calendars. |
| **Google Meet link** | Explicitly removed by the specification. Never generated; asserted in tests. |
| **Self-service password reset** | See §4.1. |
| **Admin UI for directory and holidays** | Both are script-driven today. |
| **Recurring bookings** | Never mentioned in the specification. |
| **Room equipment, seat counts, photos** | Spec field 1: "Name only - no seat counts or equipment shown." |
| **Multi-branch support** | Everything is the Mumbai branch. `BRANCH_NAME` is a label, not a partition key. |

## 6. Operational notes

**Rate limiting is per worker and in memory.** One Render worker on the free
plan, so that is the whole limit. If NM Meet is ever run with more than one
worker the limit becomes approximate per worker, and should move to the database
or Redis. It is deliberately not applied to reads: the grid polls availability
every minute.

**Sessions are stateless.** The cookie is signed, not looked up. Render restarts
a single worker freely, and a server-side store would sign everyone out on every
cold start. Rotating `SECRET_KEY` signs everyone out - that is the intended
emergency lever.

**The daily summary cron job needs a paid Render plan.** Cron jobs are not on the
free tier. `nm-meet-daily-summary` is in `render.yaml` on the `starter` plan and
is simply ignored while the account is free. Until then run
`python -m scripts.daily_summary` from any scheduler, or by hand. The job is
idempotent, so a missed day can be caught up and a retry sends nothing twice.

**Free PostgreSQL expires 30 days after creation**, with a 14-day grace period to
upgrade before Render deletes it and all its data. The free database is fine for
proving the deploy and unfit for real bookings.

**The free web service sleeps after 15 minutes** of no traffic. The first request
after that takes about a minute, plus the migration check and seed that
`start.sh` runs before uvicorn binds.

## 7. If something breaks

1. **Check `/api/health`.** It returns 503 when the database is unreachable, and
   Render's health check uses it, so a deploy that cannot see its database never
   replaces a working one.
2. **Every response carries `X-Request-ID`**, and a 500 puts that same id in the
   message shown to the user. Grep the Render log stream for it: every line of
   that request shares the id.
3. **If bookings start overlapping, check the constraint first:**
   ```sql
   SELECT conname, pg_get_constraintdef(oid)
   FROM pg_constraint WHERE conname = 'no_double_booking';
   ```
   If that returns no row, nothing above the database is preventing a double
   booking. `python -m scripts.verify_constraint` proves all of it in one go.
4. **Notifications that did not arrive** leave a row either way:
   ```sql
   SELECT recipient, event, status, error, created_at
   FROM notification_log WHERE status = 'FAILED' ORDER BY created_at DESC;
   ```
   Rows sitting at `QUEUED` for `mumbai.all@shreenm.com` are correct - they are
   waiting for the daily summary.
