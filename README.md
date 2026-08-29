# NM Meet

Meeting room booking for **Shree NM, Mumbai branch**. Five rooms - Spark, Power,
Pulse, Ignite and Switch - with availability tracked per room, never globally.

Built to `docs/NM_Meet_Booking_Documentation.pdf` v1.0. That document is the contract.
`HANDOVER.md` records every decision taken, every question still open, and what
is deliberately not built.

---

## What it does

- A day grid of all five rooms, 09:00-20:00 in 30-minute steps.
- A six-step booking wizard: room → date → start → end → details → confirm.
- **Cannot double-book.** Three layers, and the third is a PostgreSQL exclusion
  constraint that physically refuses an overlapping row.
- Cancel, edit the details, release a no-show, accept or decline an invitation.
- Notifies attendees, the conductor, reception and the booker immediately, and
  the branch group in a daily 8 am summary.
- Email and password sign-in, with permissions enforced on the server.

Mon-Sat working, Sunday closed. 30 minutes minimum, 4 hours maximum, 90 days
ahead. Times are stored as `timestamptz` in UTC and shown in Asia/Kolkata.

## Why PostgreSQL is not swappable

Spec section 7 requires three layers against a double booking and says only the
third is a guarantee:

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
`DATABASE_URL` rather than run a system that cannot keep its central promise. The
half-open `'[)'` range is exactly why a meeting ending at 15:00 does not clash
with one starting at 15:00, and `WHERE (status = 'CONFIRMED')` is why cancelling
frees the room instantly.

`python -m scripts.verify_constraint` proves all of it against a live database.

## Stack

Python 3.11 · FastAPI · SQLAlchemy 2.0 · Alembic · Pydantic v2 · PostgreSQL 16
via psycopg2 · the approved HTML/CSS/JS frontend, served from the same process.

No framework on the frontend, no build step, no bundler. `static/index.html` is
`docs/nm-meet-web_1.html` with only its data layer replaced; everything outside the
`<script>` block is byte-identical.

---

## Local setup

The Anaconda environment is assumed to be created and activated. Do not create a
venv and do not run `conda create`.

### 1. Dependencies

```bash
cd nm_meet
pip install -r requirements.txt
```

### 2. PostgreSQL 16

From conda-forge, which works on Windows and matches the version pinned in
`render.yaml`:

```bash
conda install -c conda-forge "postgresql=16" -y
postgres --version          # PostgreSQL 16.x
```

> If conda-forge ever fails, install the **EDB PostgreSQL 16** Windows installer
> from enterprisedb.com with *Command Line Tools* ticked, and skip to step 5
> using the `postgres` superuser it creates.

### 3. Create the cluster

Data lives in `.pgdata/`, inside the project and gitignored.

```powershell
$scratch = "$env:TEMP\pgpw.txt"
Set-Content -Path $scratch -Value "postgres_dev" -Encoding ascii -NoNewline
initdb -D .pgdata -U postgres --auth-host=scram-sha-256 --auth-local=scram-sha-256 --pwfile=$scratch --encoding=UTF8
Remove-Item $scratch
```

### 4. Start it

```powershell
pg_ctl -D .pgdata -l .pgdata\logfile -o "-p 5432 -h 127.0.0.1" start
```

Stop with `pg_ctl -D .pgdata stop`. It does not survive a reboot.

### 5. Role and database

```powershell
$env:PGPASSWORD = "postgres_dev"
createuser -U postgres -h 127.0.0.1 -p 5432 --createdb nm_meet
psql -U postgres -h 127.0.0.1 -p 5432 -d postgres -c "ALTER ROLE nm_meet WITH PASSWORD 'nm_meet_dev';"
createdb -U postgres -h 127.0.0.1 -p 5432 -O nm_meet nm_meet
```

`nm_meet` is deliberately **not** a superuser. It owns its database, which is
exactly what Render gives you, so `CREATE EXTENSION btree_gist` is exercised
under production privileges.

### 6. Configure

```bash
cp .env.example .env
```

Set two lines:

```
DATABASE_URL=postgresql+psycopg2://nm_meet:nm_meet_dev@127.0.0.1:5432/nm_meet
SEED_PASSWORD=nmmeet-dev
```

`SEED_PASSWORD` gives every seeded account the same password, which is what you
want on a laptop. Leave it blank and each account gets its own random one,
printed once by the seed.

### 7. Migrate, seed, verify

```bash
alembic upgrade head
python -m scripts.seed          # prints sign-in details once
python -m scripts.verify_constraint
pytest
```

`verify_constraint` must print `10/10 checks passed`. If it does not, stop - 
nothing above the database is preventing a double booking.

### 8. Run

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

| URL | |
| --- | --- |
| `http://localhost:8000/` | the app (redirects to sign-in) |
| `http://localhost:8000/api/docs` | generated API documentation |
| `http://localhost:8000/api/health` | health check |

Sign in as `priya.nair@shreenm.com` with the password the seed printed.

---

## Environment variables

Every one is documented in `.env.example`. The ones that matter:

| Key | Default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | local postgres | Render's `postgres://` scheme is rewritten to `postgresql+psycopg2://` automatically |
| `SECRET_KEY` | dev placeholder | Signs the session cookie. **The app refuses to start in production while this is the default.** Render generates one |
| `SESSION_MAX_AGE_SECONDS` | `43200` | One working day |
| `BCRYPT_ROUNDS` | `12` | |
| `SEED_PASSWORD` | blank | Blank = a random password per account, printed once |
| `ENVIRONMENT` | `development` | `production` turns on Secure cookies and the secret check |
| `TZ` | `Asia/Kolkata` | Display zone. Storage is always UTC |
| `OPEN_TIME` / `CLOSE_TIME` | `09:00` / `20:00` | |
| `SLOT_MINUTES` | `30` | D-07 |
| `MIN_BOOKING_MINUTES` / `MAX_BOOKING_MINUTES` | `30` / `240` | |
| `MAX_ADVANCE_DAYS` | `90` | |
| `NO_SHOW_RELEASE_MINUTES` | `15` | D-06 |
| `CLOSED_WEEKDAYS` | `[6]` | Monday=0. D-03: Sunday closed |
| `NOTIFICATIONS_ENABLED` | `false` | `true` **and** `SMTP_HOST` set switches to real mail; otherwise stdout |
| `SMTP_*` | blank | Host, port, user, password, STARTTLS, from address |
| `RECEPTION_EMAIL` | `reception.mumbai@shreenm.com` | |
| `MUMBAI_GROUP_EMAIL` | `mumbai.all@shreenm.com` | Daily summary only (D-01) |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Builds the "view or cancel" link in every message |
| `RATE_LIMIT_BOOKINGS` | `20` | Booking writes per user per window |

There are **no magic numbers anywhere else**. Every constant is here.

## Common tasks

**Load holidays.** Never invented, never seeded. Prepare a CSV of `date,name` - 
`holidays_sample.csv` is a starting point:

```csv
date,name
2027-01-26,Republic Day
2027-03-25,Holi
```

```bash
python -m scripts.load_holidays holidays_2027.csv --dry-run   # parse and report
python -m scripts.load_holidays holidays_2027.csv             # write
```

Safe to re-run. A date already present is updated only if its name changed.
Nothing is ever deleted, so removing a holiday is a deliberate act. The weekly
Sunday closure is computed from `CLOSED_WEEKDAYS`, never stored here.

**Add a user.** The directory is admin-maintained; there is no admin screen yet.
Add them to `DIRECTORY` in `scripts/seed.py` and re-run `python -m scripts.seed`
 - it is insert-only, so existing accounts are untouched and the new one's
password is printed once. For a one-off:

```python
from app.database import SessionLocal
from app.core.security import hash_password, generate_password
from app.models import User, UserRole

password = generate_password()
with SessionLocal() as db:
    db.add(User(full_name="New Person", email="new.person@shreenm.com",
                department_id=1, role=UserRole.EMPLOYEE, is_active=True,
                password_hash=hash_password(password)))
    db.commit()
print(password)   # give it to them, then forget it
```

To remove somebody, set `is_active = False`. Never delete: their bookings
reference them and the audit record must survive.

**Send the daily summary by hand.**

```bash
python -m scripts.daily_summary --dry-run     # print it
python -m scripts.daily_summary               # send and mark SENT
python -m scripts.daily_summary --date 2026-09-01
```

**Run one QA suite.**

```bash
pytest tests/test_spec_section_12.py -v -o addopts=""   # T01..T15
```

---

## Deployment

One Render web service, one managed PostgreSQL database, one cron job, from the
committed `render.yaml`. No Docker, no separate frontend host.

`nm_meet/` is the repository root, so `render.yaml` sits at the top of the repo.

### First deploy

1. `cd nm_meet && git init && git add . && git commit -m "NM Meet"`
2. `git update-index --chmod=+x start.sh`
3. Create an empty GitHub repository, then
   `git remote add origin git@github.com:<org>/<repo>.git && git push -u origin main`
4. Render → **New** → **Blueprint** → connect the repository → **Apply**
5. Render prompts for the `sync: false` values. Set `PUBLIC_BASE_URL` to the
   service URL. Leave the SMTP keys blank until a relay exists - notifications
   go to stdout until then, and are still recorded.

`SECRET_KEY` is generated by Render and never enters git. `DATABASE_URL` is wired
from the database automatically.

### Every deploy runs

```
pip install -r requirements.txt      build
alembic upgrade head                 start.sh - exits non-zero on failure
python -m scripts.seed               start.sh - idempotent
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

`start.sh` runs under `set -euo pipefail`, so a failed migration fails the deploy
and Render keeps the previous version serving.

### Verify a deploy

```bash
curl -i https://<service>.onrender.com/api/health      # 200, database connected
```

In the deploy log, confirm the migration and seed lines. Then, in the database
shell (dashboard → `nm-meet-db` → **Connect** → PSQL command):

```sql
SELECT conname, pg_get_constraintdef(oid)
FROM pg_constraint WHERE conname = 'no_double_booking';
```

If that returns no row the deploy is not usable, whatever the health check says.

### Free-tier limits

- The web service **sleeps after 15 minutes** without traffic; waking takes about
  a minute, plus migration and seed.
- A free PostgreSQL database **expires 30 days after creation**, with a 14-day
  grace period before Render deletes it and its data. Move to a paid plan before
  real bookings exist.
- **Cron jobs are a paid feature.** `nm-meet-daily-summary` is defined in
  `render.yaml` and ignored on the free plan. Run the script by hand or from
  another scheduler until the account is upgraded.

---

## Layout

```
nm_meet/
├── app/
│   ├── main.py              app, middleware, exception handlers, static mount
│   ├── config.py            every setting and constant in the system
│   ├── database.py          engine, session factory, declarative Base
│   ├── models/              SQLAlchemy models (spec section 11)
│   ├── schemas/             Pydantic request/response models
│   ├── api/                 auth, reference, availability, bookings, health
│   ├── services/            availability, booking, lifecycle, notifications,
│   │                        transports, permissions, audit
│   └── core/                auth, security, messages, time, errors, middleware,
│                            logging
├── alembic/versions/        0001 schema + constraint, 0002 passwords
├── scripts/                 seed, verify_constraint, load_holidays, daily_summary
├── static/                  index.html (the approved prototype), login.html
├── tests/                   150 tests, including T01-T15 as a named suite
├── render.yaml              web service + database + daily summary cron
├── start.sh                 migrate → seed → serve
└── HANDOVER.md              decisions, open questions, what is not built
```

## Conventions

- **All times are `timestamptz` in UTC**, displayed in Asia/Kolkata. `"13:00"` is
  never stored as text. Conversion lives only in `app/core/time.py`.
- **Every user-facing string is in `app/core/messages.py`.** Spec section 10 rules
  out "Invalid selection" and "Booking failed"; the tests assert exact strings,
  not status codes.
- **Times in messages are written "1 pm", "1:30 pm"** - one formatter,
  `format_clock`, used everywhere.
- **The actor always comes from `get_current_user`**, never from a request body.
- **Bookings are never deleted.** `CANCELLED` and `NO_SHOW` both free the room
  instantly, because the exclusion constraint binds `CONFIRMED` rows only.
- **API routes are registered before the `StaticFiles` mount.** The mount at `/`
  is a catch-all and silently shadows anything added after it.
