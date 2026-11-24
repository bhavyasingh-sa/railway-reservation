# RailBook — IRCTC-style Railway Reservation System

A train ticket reservation system modelled on IRCTC's
real booking rules — Confirmed / RAC / Waitlist / Tatkal — built on MySQL
with a Flask + Bootstrap frontend.

## Features and DBMS concepts
- **Schema design**: 12 normalized tables (stations, cities, trains, routes,
  classes, quotas, train_class_quota, seats, users, bookings, passengers,
  payments)
- **Trigger**: `trg_booking_window_check` enforces, at the database level,
  the advance-reservation window, the Tatkal opening time (10:00 the day
  before) and "no Tatkal on the journey day", that the destination comes
  after the boarding station, and chair-car-only classes on Tejas/Vande Bharat
  ([sql/01_create_tables_trigger_views.sql](sql/01_create_tables_trigger_views.sql))
- **Stored procedures**: `sp_add_passenger` (segment-overlap seat search →
  seat → RAC → waitlist fallback) and `sp_cancel_passenger` (the
  RAC→Confirmed / Waitlist→RAC cascade) ([sql/02_stored_procedures.sql](sql/02_stored_procedures.sql))
- **Transactions + row locking**: `SELECT ... FOR UPDATE` inside the
  booking procedure (see the known limitation under *Notes on scope*)
- **Views**: `vw_train_occupancy`, `vw_revenue_by_train` power the admin
  dashboard charts
- **CTEs + window functions**: city-grouped search
  ([sql/04_add_city_grouping.sql](sql/04_add_city_grouping.sql)) uses `ROW_NUMBER()` over two CTEs
  to resolve, per train, the best boarding/alighting station within a city
  that has multiple real stations (e.g. Lucknow = LKO + LJN)
- **Search filters**: the results page can filter by class, departure-time
  window and max fare, sort by fare, departure or journey duration, and hide
  waitlisted options. One SQL query in `search()` ([app.py](app.py)) does the
  work: CTEs + window functions find each train's trip between the two cities,
  `WHERE` filters the departure window, a join to `classes`/`quotas`/
  `train_class_quota` computes the fare for every class and quota, the class
  and max-fare filters are applied in that query, and `ORDER BY` sorts
  (cheapest fare via `MIN() OVER (PARTITION BY train)`). Only seat availability
  and "hide waitlisted" are checked afterwards, per option.

## Real data
Train/station/route master data (5,382 trains, 9,006 stations, ~415,000 route
stops with arrival/departure times, grouped into 7,445 cities) lives in the
MySQL tables `trains`, `stations`, `routes` and `cities`. It comes from two
sources, loaded once:

1. **Open dataset CSVs** in [data/indian_railways_dataset/](data/indian_railways_dataset/)
   (`trains.csv`, `stations.csv`, `schedules.csv`), from
   https://github.com/anandology/railways (originally gathered by
   datameet/railways; licence CC0, public domain). About 5,200 trains.
   Caveats: this dataset dates from 2016 (so no Tejas / Vande Bharat and some
   old train numbers), and it has no per-stop distance, so distances were
   estimated from the stations' coordinates scaled to each train's total
   distance.
2. **RailRadar API** (https://railradar.in, free tier of 100 calls): the
   newest trains, which the 2016 data lacks. All 8 Tejas and 172 Vande Bharat
   trains were added; 91 of them (the Tejas trains and the most popular Vande
   Bharat) have every stop with live timings, the rest have origin to
   destination only. Four Rajdhani/Shatabdi trains (12003, 12004, 12423, 12424)
   and 12314/12302 were also refreshed with live routes. The raw API responses
   are kept locally and are not part of this repository.

All booking/RAC/waitlist/Tatkal logic is entirely our own, independent of
these data sources. Stations of one real city are grouped (e.g. "Lucknow" =
LKO + LJN + LC + Alambagh; "New Delhi" = New Delhi, Old Delhi, Hazrat
Nizamuddin, Anand Vihar, Sarai Rohilla, Shahdara...) so search works by city,
RailYatri-style, instead of requiring the exact station. Grouping rule:
stations whose names start with the same word and lie within 30 km of each
other, plus a short hand-written list for big cities (Delhi, Hyderabad +
Secunderabad, Bengaluru, Prayagraj, Varanasi...). The one-time import scripts
have been removed from the project; the app itself never calls any API.

## SQL files (in `sql/`)

| File | What it is for |
|---|---|
| `01_create_tables_trigger_views` | Creates the database, all tables, the booking-window trigger and the 2 dashboard views |
| `02_stored_procedures` | Booking and cancellation logic (confirmed / RAC / waitlist) |
| `03_seed_classes_and_quotas` | Starter data: classes, quotas, and the seat generator for whatever trains exist |
| `04_add_city_grouping` | Adds the `cities` table so search works by city |

## Setup

1. Create the database user (one-time, needs your MySQL root password):
   ```bash
   /usr/local/mysql/bin/mysql -u root -p -e "
   CREATE DATABASE IF NOT EXISTS railway_reservation;
   CREATE USER IF NOT EXISTS 'railway_app'@'localhost' IDENTIFIED BY 'choose-a-strong-password';
   GRANT ALL PRIVILEGES ON railway_reservation.* TO 'railway_app'@'localhost';
   FLUSH PRIVILEGES;
   SET GLOBAL log_bin_trust_function_creators = 1;
   "
   ```
   (`log_bin_trust_function_creators` is needed once per MySQL restart so the
   app user can create the trigger/procedures.)

2. Load the schema — run `01`, `02`, `03`, `04` in that order; together they
   build the complete database.
   ```bash
   cd railway-reservation-dbms
   /usr/local/mysql/bin/mysql -u railway_app -p < sql/01_create_tables_trigger_views.sql
   /usr/local/mysql/bin/mysql -u railway_app -p < sql/02_stored_procedures.sql
   /usr/local/mysql/bin/mysql -u railway_app -p < sql/03_seed_classes_and_quotas.sql
   /usr/local/mysql/bin/mysql -u railway_app -p < sql/04_add_city_grouping.sql
   ```

3. Install Python deps and run:
   ```bash
   pip3 install -r requirements.txt
   python3 app.py
   ```
   Create a `.env` file in the project folder first (it is git-ignored):
   ```
   DB_HOST=localhost
   DB_USER=railway_app
   DB_PASSWORD=<the password you chose in step 1>
   DB_NAME=railway_reservation
   SECRET_KEY=<64 random characters: python3 -c "import secrets; print(secrets.token_hex(32))">
   APP_DEBUG=1        # local development only, never in production
   ```
   The app refuses to start if `SECRET_KEY` or `DB_PASSWORD` is missing, or if
   the key is a placeholder. App runs at http://localhost:5001

   Note: the real train data (5,382 trains with timings) lives in the live
   MySQL database, not in these scripts, and `sql/01_create_tables_trigger_views.sql`
   drops and recreates the database, so running it wipes that data. A fresh
   install from these files has no trains, stations or cities at all. To get the
   data back, restore a dump of the database (a `backup/` folder holds one; it is
   git-ignored because it is large) as the MySQL root user:
   ```bash
   /usr/local/mysql/bin/mysql -u root -p -e "DROP DATABASE IF EXISTS railway_reservation; CREATE DATABASE railway_reservation;"
   /usr/local/mysql/bin/mysql -u root -p railway_reservation < backup/railway_reservation_<date>.sql
   ```

## Accounts
There are no built-in logins. Register through the app, then make one account
an admin (needed for the dashboard):
```bash
python3 -m flask --app app make-admin you@example.com
```

## Security
- Passwords are salted-hashed (`werkzeug`), never stored in plain text; 8+
  characters required.
- Every SQL query uses bound `%s` parameters (no string-built SQL from user input).
- All POST forms carry a per-session CSRF token; requests without it get a 400.
- Login is rate-limited (5 failures per email+IP per 15 minutes, 30 per IP);
  sign-ups are limited to 10 per IP per hour. Counters live in server memory
  (per process), which is fine for a small deployment.
- Sessions use signed, `HttpOnly`, `SameSite=Lax` cookies that expire after 12
  hours, and are `Secure` (HTTPS-only) unless `APP_DEBUG=1`. A fresh session is
  created on login.
- A PNR page is visible only to the account that made the booking (and
  admins). Cancelling checks ownership. The dashboard is admin-only.
- After login, `next` redirects only to same-site paths. Responses send
  `X-Content-Type-Options`, `X-Frame-Options` and `Referrer-Policy` headers.
- Not implemented: email verification, password reset, OAuth ("Sign in with
  Google"), and account lockout beyond the rate limit.

## Deploying
Don't use `python3 app.py` (the development server). Set `SECRET_KEY` and the
DB variables as environment variables on the host, leave `APP_DEBUG` unset,
serve over HTTPS, and run:
```bash
gunicorn app:app
```
Use a strong MySQL password, don't expose port 3306 to the internet, and if
the host puts the app behind a proxy, rate limiting sees the proxy's address
unless you add `werkzeug.middleware.proxy_fix.ProxyFix`.

## Notes on scope
- Seat pools are set intentionally small (6 general + 2 RAC seats, 3 Tatkal
  seats per train/class) so RAC and waitlist behaviour is easy to trigger and test
  without needing hundreds of bookings.
- Fares are a simplified model, not real IRCTC tariffs: `total = (base_fare +
  distance_km × fare_per_km) × quota_multiplier × surge + reservation_charge`
  (`classes` table in [sql/01_create_tables_trigger_views.sql](sql/01_create_tables_trigger_views.sql), `compute_fare()`
  in [app.py](app.py)). Real IRCTC pricing uses distance slabs, GST, and
  per-class charges that differ by train type.
- Tejas/Vande Bharat trains (matched by name, see `is_premium_train()` in
  [app.py](app.py)) only offer Chair Car (CC) / Executive Chair Car (EC) —
  never SL/3A/2A — enforced both in the UI (`search()` picks `PREMIUM_CLASSES`
  vs `CLASSES` per train) and by the `trg_booking_window_check` trigger in
  both directions ([sql/01_create_tables_trigger_views.sql](sql/01_create_tables_trigger_views.sql)).
  Their fares never price below `PREMIUM_MIN_FARE` (₹1000). Chair car seats
  have no berth (`seats.berth_type` is `NULL`), so the booking form hides
  the berth-preference dropdown for them.
- Berth type (`seats.berth_type`) follows real coach layout: SL/3A cycle
  Lower/Middle/Upper, 2A alternates Lower/Upper (no Middle), and RAC seats
  are Side Lower — matching how RAC sharing actually works. Passengers can
  request a `berth_preference`, which `sp_add_passenger` tries to honor
  (best-effort, like real IRCTC) before falling back to any available seat.
- `passengers.food_preference` (Veg/Non-veg/No Food) is stored per person
  and shown on the ticket.
- Tatkal rules: it appears in search only when the journey is today or
  tomorrow. It opens at 10:00 the day before (all classes; real IRCTC opens
  Sleeper at 11:00), shows REGRET on the journey day, and has no RAC or
  waitlist: if a booking can't be fully confirmed it is undone with a REGRET
  message. Tejas/Vande Bharat have no Tatkal quota.
- **Simulated demand**: on the real app other people buy Tatkal seats as soon
  as it opens; we only have this app's own bookings. So each train+class+date
  gets a fixed sell-out time (20 minutes to ~23 hours after opening) and the
  seats are marked as taken by other buyers gradually until then
  (`tatkal_simulated_held()` in [app.py](app.py); set
  `TATKAL_SIMULATED_DEMAND = False` to turn it off). These phantom buyers only
  reduce availability; they are not bookings.
- Cancelling is per passenger ("Cancel ticket" on each row); the PNR is closed
  and the payment marked REFUNDED when the last passenger is cancelled.
- Known limitation: two people booking the same seat at the same instant can
  both be given it, because the seat-free check in `sp_add_passenger` reads an
  older snapshot. The fix would be to lock the `train_class_quota` row at the
  start of the procedure and use `READ COMMITTED`. Not done: one user at a time
  can't trigger it. Payment is also a stub (always succeeds).
- Arrival/departure times per stop (`routes.arrival_time`/`departure_time`
  + a day-offset column for multi-day trains) are shown on search results
  and the ticket. Every train's times come from real timetable data (the open dataset, or
  RailRadar API for the newest trains) and are stored in `routes`.
