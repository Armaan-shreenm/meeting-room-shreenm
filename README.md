# NM Meet

Meeting room booking for **Shree NM, Mumbai branch**. Five rooms — Spark, Power,
Pulse, Ignite and Switch — with availability tracked per room, never globally.

Built to `NM_Meet_Booking_Documentation.pdf` v1.0. That document is the contract.

**Phase 1 is complete and verified.** The schema, the `btree_gist` extension and
the `no_double_booking` exclusion constraint have been proven against a real
PostgreSQL 16 — see [Proving the constraint](#proving-the-constraint). There are
no booking endpoints, no availability logic, no notifications and no
authentication yet.

---

## Stack

| Layer      | Choice                                                    |
| ---------- | --------------------------------------------------------- |
| Runtime    | Python 3.11                                               |
| API        | FastAPI, Pydantic v2                                      |
| ORM        | SQLAlchemy 2.0, typed `mapped_column` style               |
| Migrations | Alembic                                                   |
| Database   | PostgreSQL 16 via `psycopg2-binary` — **not swappable**   |
| Frontend   | The approved HTML/CSS/JS prototype, served from `static/` |
| Hosting    | One Render Web Service + one Render PostgreSQL database    |

### Why PostgreSQL is not a choice

Section 7 of the specification requires three layers of protection against a
double booking, and states plainly that only the third is a guarantee:

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;

ALTER TABLE bookings
ADD CONSTRAINT no_double_booking
EXCLUDE USING gist (
    room_id WITH =,
    tstzrange(entry_time, exit_time, '[)') WITH &&
) WHERE (status = 'CONFIRMED');
```

No other engine has this. `config.py` refuses to start against a non-PostgreSQL
`DATABASE_URL` rather than let the system run without its central promise.

---

## Settled decisions

Recorded here because they are not all in the PDF.

| Area | Decision |
| --- | --- |
| Ids | `bookings`, `booking_attendees`, `notification_log`, `audit_log` use application-generated **UUID4**. Rooms keep their slug id; departments and users keep integer ids. |
| Attendee reply | `attendee_response` enum: `PENDING` \| `ACCEPTED` \| `DECLINED`, default `PENDING`. |
| Notification delivery | `notification_status` enum: `QUEUED` \| `SENT` \| `FAILED`, default `QUEUED`. |
| Authentication | Deferred to Phase 5. `app/core/auth.py` resolves the actor from the `X-User-Email` header, falling back to `DEV_USER_EMAIL`. Every endpoint takes the actor from that dependency, never from the request body. |
| Directory | Admin-maintained and seeded. No HR sync. One `ADMIN` (`admin@shreenm.com`) and one `RECEPTION` user. |
| Editing a booking | Details only — title, department, conducted_by, attendees, reception note. That fires `CHANGED`. **Room, date and time are never editable**; changing them is a cancel and a re-book. |
| No-show | Reception or admin only. Sets `status = NO_SHOW`, which frees the room immediately because the constraint binds `CONFIRMED` rows only. Audit-logged, no notification. |
| Holidays | Reception and admin maintain them. Never seeded — `scripts/load_holidays.py` imports a CSV. The Sunday closure is computed from `CLOSED_WEEKDAYS`, never stored as rows. |

---

## Project layout

```
nm_meet/
├── app/
│   ├── main.py              FastAPI app, static mount, router wiring
│   ├── config.py            every setting and constant in the system
│   ├── database.py          engine, session factory, declarative Base
│   ├── models/              SQLAlchemy models (spec section 11)
│   ├── schemas/             Pydantic request/response models
│   ├── api/                 HTTP routes, all mounted under /api
│   ├── services/            business logic (added in a later phase)
│   └── core/
│       ├── auth.py          get_current_user — the seam real auth drops into
│       ├── logging.py       stdout only; the Render disk is ephemeral
│       └── time.py          the only UTC ⇄ Asia/Kolkata conversion
├── alembic/
│   ├── env.py               reads the URL from app.config, never alembic.ini
│   └── versions/0001_initial_schema.py
├── scripts/
│   ├── seed.py              rooms, departments, directory — idempotent
│   ├── load_holidays.py     import declared holidays from a CSV
│   └── verify_constraint.py prove the double-booking guarantee
├── tests/
│   └── test_booking_date.py booking_date must agree with entry_time
├── static/index.html        placeholder; the prototype lands here later
├── render.yaml              Render Blueprint: web service + database
├── start.sh                 migrate → seed → serve
└── requirements.txt         pinned
```

---

## Local setup

The Anaconda environment is assumed to be created and activated already. Do not
create a venv and do not run `conda create`.

### 1. Install Python dependencies

```bash
cd nm_meet
pip install -r requirements.txt
```

### 2. Install PostgreSQL 16

Taken from conda-forge, which works on Windows and matches the
`postgresMajorVersion: "16"` pinned in `render.yaml`:

```bash
conda install -c conda-forge "postgresql=16" -y
postgres --version          # PostgreSQL 16.x
```

> If conda-forge ever fails on your machine, install the **EDB PostgreSQL 16**
> Windows installer from enterprisedb.com instead, tick *Command Line Tools*, and
> skip to step 4 using the `postgres` superuser the installer creates.

### 3. Create the cluster

The data directory lives inside the project and is gitignored (`.pgdata/`).

```bash
# PowerShell, from nm_meet/
$scratch = "$env:TEMP\pgpw.txt"
Set-Content -Path $scratch -Value "postgres_dev" -Encoding ascii -NoNewline

initdb -D .pgdata -U postgres --auth-host=scram-sha-256 --auth-local=scram-sha-256 --pwfile=$scratch --encoding=UTF8

Remove-Item $scratch
```

### 4. Start the server

```bash
pg_ctl -D .pgdata -l .pgdata\logfile -o "-p 5432 -h 127.0.0.1" start
```

Stop it later with `pg_ctl -D .pgdata stop`. It does not survive a reboot; run
the start command again.

### 5. Create the role and database

```bash
$env:PGPASSWORD = "postgres_dev"
createuser -U postgres -h 127.0.0.1 -p 5432 --createdb nm_meet
psql -U postgres -h 127.0.0.1 -p 5432 -d postgres -c "ALTER ROLE nm_meet WITH PASSWORD 'nm_meet_dev';"
createdb -U postgres -h 127.0.0.1 -p 5432 -O nm_meet nm_meet
```

`nm_meet` is deliberately **not** a superuser. It owns its database, which is
exactly what Render gives you, so `CREATE EXTENSION btree_gist` is tested here
under the same privileges it will have in production.

### 6. Configure

```bash
cp .env.example .env
```

Set one line:

```
DATABASE_URL=postgresql+psycopg2://nm_meet:nm_meet_dev@127.0.0.1:5432/nm_meet
```

### 7. Migrate, seed, verify

```bash
alembic upgrade head
python -m scripts.seed
python -m scripts.verify_constraint
pytest -v
```

`verify_constraint` must print `10/10 checks passed`. If it does not, stop —
nothing above the database prevents a double booking.

### 8. Run

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

| URL | What it is |
| --- | --- |
| `http://localhost:8000/` | the frontend (placeholder today) |
| `http://localhost:8000/api/health` | health check |
| `http://localhost:8000/api/docs` | generated API documentation |

### Loading holidays

Never invented, never seeded. Prepare a CSV of `date,name`:

```csv
date,name
2027-01-26,Republic Day
2027-03-25,Holi
```

```bash
python -m scripts.load_holidays holidays_2027.csv --dry-run   # parse and report
python -m scripts.load_holidays holidays_2027.csv             # write
```

Re-importing is safe. Nothing is ever deleted; removing a holiday is a
deliberate act.

---

## Proving the constraint

`scripts/verify_constraint.py` writes real rows to a real PostgreSQL and
requires the database to refuse the overlapping ones. Every rejection must be a
`psycopg2.errors.ExclusionViolation` specifically — a bare `except` would pass on
a typo and prove nothing. All probe rows are removed afterwards, and the script
exits non-zero if any check fails.

| # | Check |
| --- | --- |
| 1 | `btree_gist` present in `pg_extension` |
| 2 | `no_double_booking` present in `pg_constraint` |
| 3 | Power 13:00–15:00 inserts |
| 4 | Power 14:00–16:00 **rejected** as an overlap |
| 5 | Power 12:00–13:00 succeeds — ends exactly when the other starts (T-03) |
| 6 | Power 15:00–16:00 succeeds — starts exactly when the other ends (T-04) |
| 7 | Pulse 13:00–15:00 succeeds — different room, same window (T-02) |
| 8 | Cancel the Power row, re-insert the identical window — succeeds |
| 9 | `alembic upgrade head` a second time is a clean no-op |
| 10 | `scripts/seed.py` a second time creates no duplicates |

### booking_date integrity

`booking_date` and `entry_time` are stored independently and can drift. A CHECK
constraint cannot enforce the relationship, because PostgreSQL refuses
`AT TIME ZONE` in a CHECK — it is STABLE, not IMMUTABLE. The guarantee is made in
Python instead:

- `app/core/time.booking_date_for()` is the only thing that derives the value.
- A `before_flush` hook on the SQLAlchemy `Session` derives it when it is missing
  and raises `BookingDateMismatchError` when it disagrees.
- `tests/test_booking_date.py` covers it, including a 19:00 UTC booking that is
  00:30 the **next day** in Mumbai — the case that would file a booking under the
  wrong day and make it vanish from the grid.

---

## Render deployment

One web service and one PostgreSQL database, both on free plans, from a
committed Blueprint. No Docker, no separate frontend host, no paid features.

### 1. Push to GitHub

`render.yaml` must sit at the **root of the repository**, so the contents of
`nm_meet/` become the repository root:

```bash
cd nm_meet
git init
git add .
git commit -m "NM Meet phase 1: foundation, schema and verified constraint"
git branch -M main
git remote add origin git@github.com:<org>/<repo>.git
git push -u origin main
```

Confirm the executable bit survived a Windows checkout:

```bash
git update-index --chmod=+x start.sh
```

`.gitattributes` forces LF on `*.sh`; a CRLF shebang is the most common cause of
a first-deploy failure from Windows. `render.yaml` also invokes `bash ./start.sh`
rather than `./start.sh`, so a lost executable bit is survivable either way.

### 2. Create the Blueprint

1. Render dashboard → **New** → **Blueprint**.
2. Connect the repository. Render reads `render.yaml`.
3. It shows one web service (`nm-meet`) and one database (`nm-meet-db`).
4. Render prompts for the `sync: false` values — the SMTP settings, the reception
   and branch mail addresses, and `DEV_USER_EMAIL`. Leave them blank for now;
   `NOTIFICATIONS_ENABLED` is `false` and no Phase 1 endpoint reads them.
5. **Apply**.

`DATABASE_URL` is wired automatically from the database to the service through
`fromDatabase`. You never copy a connection string, and no secret is committed.

### 3. What happens on every deploy

```
pip install -r requirements.txt      build command
alembic upgrade head                 start.sh — exits non-zero on failure
python -m scripts.seed               start.sh — idempotent
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

`start.sh` runs under `set -euo pipefail`. If a migration fails the process exits
non-zero, Render marks the deploy failed and keeps the previous version serving.

### 4. Verify the first deploy

**a. Health.**

```bash
curl -i https://<your-service>.onrender.com/api/health
```

Expect `200` and `"database": {"connected": true, ...}`. A `503` means the app is
up but cannot see its database — check both are in the **same region**
(`singapore`).

**b. Deploy log lines.** In the Render deploy log, confirm all four:

```
==> Applying database migrations
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, Initial schema, ...
==> Seeding reference data (rooms, departments, directory)
==> Serving on 0.0.0.0:10000
```

**c. The constraint exists in production too.** Render dashboard → `nm-meet-db` →
**Connect** → copy the PSQL command, then:

```sql
SELECT conname, pg_get_constraintdef(oid)
FROM pg_constraint WHERE conname = 'no_double_booking';
```

It must return the `EXCLUDE USING gist (...) WHERE (status = 'CONFIRMED')` row.
If it is missing the deploy is not usable, whatever the health check says.

**d. Redeploy once** and confirm the second deploy also goes green. That is what
proves the migration and seed are safe on every deploy from here on.

### Free-tier behaviour

From Render's own documentation, checked 29 August 2026:

- A free web service **spins down after 15 minutes** without inbound traffic.
  Spinning back up "takes about one minute" — and this app also runs the
  migration check and the seed before uvicorn binds, so budget slightly more.
- A **free PostgreSQL database expires 30 days after creation.** There is then a
  14-day grace period to upgrade to a paid compute plan; after that Render
  deletes the database and all its data.

The 30-day expiry means the free database is fine for proving the deploy and
useless for real bookings. It must move to a paid plan before go-live.

### The daily 8 am summary is not built here

D-01 settles the Mumbai branch list on a daily 8 am summary rather than a mail
per booking. That will be a **separate Render Cron Job** added to this blueprint
in the notifications phase. It is deliberately not an in-process scheduler: this
service sleeps, and a background thread cannot be relied on to be alive at
08:00 IST.

---

## Conventions

- **All times are `timestamptz` stored in UTC**, displayed in `Asia/Kolkata`.
  `"13:00"` is never stored as text. Conversion lives in `app/core/time.py`.
- **Every constant lives in `app/config.py`.** No magic numbers elsewhere.
- **Bookings are never deleted.** `CANCELLED` and `NO_SHOW` both release the room
  instantly, because the exclusion constraint binds `CONFIRMED` rows only.
- **The actor always comes from `get_current_user`,** never from a request body.
- **API routes are registered before the `StaticFiles` mount.** The mount at `/`
  is a catch-all and silently shadows anything added after it.
