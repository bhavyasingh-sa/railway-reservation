import hmac
import os
import re
import secrets
import threading
import time
import zlib
from datetime import date, datetime, timedelta
from functools import wraps

import click
import pymysql
import pymysql.cursors
from dotenv import load_dotenv
from flask import (Flask, abort, g, flash, redirect, render_template, request,
                    session, url_for, jsonify)
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

DEBUG = os.getenv("APP_DEBUG") == "1"  # local development only; never set in production

app = Flask(__name__)

_secret = os.getenv("SECRET_KEY", "")
if len(_secret) < 32 or _secret.startswith("change-this") or _secret == "dev-secret-key":
    raise SystemExit(
        "SECRET_KEY is missing or too weak. Put a long random value in .env, e.g.\n"
        '  python3 -c "import secrets; print(secrets.token_hex(32))"'
    )
if not os.getenv("DB_PASSWORD"):
    raise SystemExit("DB_PASSWORD is not set. Put it in .env.")
app.secret_key = _secret
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=not DEBUG,  # cookie only over HTTPS in production
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)

CLASSES = ["SL", "3A", "2A"]
PREMIUM_CLASSES = ["CC", "EC"]  # Tejas/Vande Bharat: chair car only, no berths
QUOTAS = ["GN", "TQ"]


PREMIUM_MIN_FARE = 1000  # real Tejas/Vande Bharat fares are never under this

DEPARTURE_SLOTS = {
    "early":     ("Early morning (00-06)", "(fm.dep_time >= '00:00:00' AND fm.dep_time < '06:00:00')"),
    "morning":   ("Morning (06-12)",       "(fm.dep_time >= '06:00:00' AND fm.dep_time < '12:00:00')"),
    "afternoon": ("Afternoon (12-18)",     "(fm.dep_time >= '12:00:00' AND fm.dep_time < '18:00:00')"),
    "night":     ("Evening / night (18-24)", "(fm.dep_time >= '18:00:00' AND fm.dep_time <= '23:59:59')"),
}
SORT_SQL = {
    "price": "cheapest",
    "departure": "trip.dep_time IS NULL, trip.dep_time",
    "duration": "(trip.arr_time IS NULL OR trip.dep_time IS NULL), (trip.arr_day * 86400 + TIME_TO_SEC(trip.arr_time)) - (trip.dep_day * 86400 + TIME_TO_SEC(trip.dep_time))",
}


def is_premium_train(train_name):
    """Real Tejas/Vande Bharat trains: chair car only (CC/EC), and fares start high."""
    return "tejas" in train_name.lower() or "vande bharat" in train_name.lower()


def format_berth(berth_type):
    """Chair car seats have no berth_type (NULL) -- nothing to show for those."""
    return f" ({berth_type.replace('_', ' ').title()})" if berth_type else ""


def format_hhmm(value):
    """PyMySQL returns TIME columns as timedelta (or None at origin/terminus)."""
    if value is None:
        return None
    total_minutes = int(value.total_seconds() // 60)
    return f"{(total_minutes // 60) % 24:02d}:{total_minutes % 60:02d}"


def compute_fare(distance_km, cls, quota_multiplier, surge=1.0, is_premium=False):
    """(base fare + distance*rate) is subject to quota/surge (Tatkal premium,
    dynamic pricing); the reservation charge is a flat fee on top, same as
    IRCTC's reservation charge not changing for Tatkal bookings. Premium
    trains (Tejas/Vande Bharat) never price below PREMIUM_MIN_FARE."""
    seat_fare = (float(cls["base_fare"]) + distance_km * float(cls["fare_per_km"])) * quota_multiplier * surge
    fare = seat_fare + float(cls["reservation_charge"])
    if is_premium:
        fare = max(fare, PREMIUM_MIN_FARE)
    return round(fare, 2)


def get_db():
    if "db" not in g:
        g.db = pymysql.connect(
            host=os.getenv("DB_HOST", "localhost"),
            user=os.getenv("DB_USER", "railway_app"),
            password=os.environ["DB_PASSWORD"],
            database=os.getenv("DB_NAME", "railway_reservation"),
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        if exc is None:
            db.commit()
        else:
            db.rollback()
        db.close()


def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in first.", "warning")
            return redirect(url_for("login", next=request.full_path))
        return f(*args, **kwargs)
    return wrapped


def admin_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            flash("Admin access only.", "danger")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapped


def signal_message(exc):
    """Extract the human-readable message from a MySQL SIGNAL error."""
    if exc.args and len(exc.args) > 1:
        return str(exc.args[1])
    return str(exc)


# ------------------------------------------------------------ security ----

def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def csrf_protect():
    # Every state-changing request must carry the token that was rendered into
    # the form, so another website can't submit forms on a logged-in user's behalf.
    if request.method == "POST":
        expected = session.get("_csrf")
        sent = request.form.get("csrf_token", "")
        if not expected or not hmac.compare_digest(sent, expected):
            abort(400, "Your form expired or was invalid. Go back, reload the page and try again.")


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


_attempts = {}
_attempts_lock = threading.Lock()


def rate_limited(key, limit, window_seconds):
    """True if `key` already has `limit` recorded attempts in the last window."""
    now = time.time()
    with _attempts_lock:
        hits = [t for t in _attempts.get(key, []) if now - t < window_seconds]
        _attempts[key] = hits
        return len(hits) >= limit


def record_attempt(key):
    with _attempts_lock:
        _attempts.setdefault(key, []).append(time.time())


def clear_attempts(key):
    with _attempts_lock:
        _attempts.pop(key, None)


def safe_next(target):
    """Only follow same-site relative paths after login (blocks open redirects)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("index")


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.cli.command("make-admin")
@click.argument("email")
def make_admin(email):
    """Give an existing account admin rights: flask --app app make-admin you@example.com"""
    db = get_db()
    email = email.strip().lower()
    with db.cursor() as cur:
        cur.execute("SELECT user_id FROM users WHERE email=%s", (email,))
        if not cur.fetchone():
            click.echo("No account with that email.")
            return
        cur.execute("UPDATE users SET is_admin=1 WHERE email=%s", (email,))
    click.echo("Done: that account is now an admin.")


# ---------------------------------------------------------------- auth ----

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        reg_key = f"register:{request.remote_addr}"
        if rate_limited(reg_key, 10, 3600):
            flash("Too many sign-ups from this address. Try again later.", "danger")
            return redirect(url_for("register"))
        record_attempt(reg_key)
        if not (1 <= len(name) <= 100):
            flash("Enter your name (up to 100 characters).", "danger")
            return redirect(url_for("register"))
        if len(email) > 120 or not EMAIL_RE.match(email):
            flash("Enter a valid email address.", "danger")
            return redirect(url_for("register"))
        if not (8 <= len(password) <= 128):
            flash("Password must be 8 to 128 characters.", "danger")
            return redirect(url_for("register"))
        db = get_db()
        with db.cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE email=%s", (email,))
            if cur.fetchone():
                flash("An account with that email already exists.", "danger")
                return redirect(url_for("register"))
            cur.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (%s,%s,%s)",
                (name, email, generate_password_hash(password)),
            )
        flash("Account created. Please log in.", "success")
        return redirect(url_for("login"))
    return render_template("app.html", page="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        pair_key = f"login:{request.remote_addr}:{email}"
        ip_key = f"login-ip:{request.remote_addr}"
        if rate_limited(pair_key, 5, 900) or rate_limited(ip_key, 30, 900):
            flash("Too many failed attempts. Please wait 15 minutes and try again.", "danger")
            return render_template("app.html", page="login"), 429
        db = get_db()
        with db.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email=%s", (email,))
            user = cur.fetchone()
        if user and check_password_hash(user["password_hash"], password):
            clear_attempts(pair_key)
            session.clear()  # new session on login, so an old cookie can't be reused
            session.permanent = True
            session["user_id"] = user["user_id"]
            session["name"] = user["name"]
            session["is_admin"] = bool(user["is_admin"])
            flash(f"Welcome back, {user['name']}!", "success")
            return redirect(safe_next(request.args.get("next")))
        record_attempt(pair_key)
        record_attempt(ip_key)
        flash("Invalid email or password.", "danger")
    return render_template("app.html", page="login")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("index"))


# -------------------------------------------------------------- search ----

@app.route("/")
def index():
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT c.city_id, c.name
            FROM cities c JOIN stations s ON s.city_id = c.city_id
            ORDER BY c.name
            """
        )
        cities = cur.fetchall()
    min_date = date.today().isoformat()
    return render_template("app.html", page="index", cities=cities, min_date=min_date)


def seat_availability(cur, tcq_id, journey_date, from_seq, to_seq):
    """Mirrors sp_add_passenger's logic, but counts instead of assigning."""
    cur.execute(
        """
        SELECT COUNT(*) AS n FROM seats s
        WHERE s.tcq_id=%s AND s.is_rac_seat=0
          AND NOT EXISTS (
              SELECT 1 FROM passengers pp
              JOIN bookings bb ON bb.pnr_id = pp.pnr_id
              WHERE pp.seat_id = s.seat_id AND pp.seat_status='CONFIRMED'
                AND bb.journey_date=%s
                AND NOT (bb.to_seq <= %s OR bb.from_seq >= %s)
          )
        """,
        (tcq_id, journey_date, from_seq, to_seq),
    )
    available = cur.fetchone()["n"]

    cur.execute(
        """
        SELECT s.seat_id,
               SUM(CASE WHEN pp.seat_status='RAC' AND bb.pnr_id IS NOT NULL THEN 1 ELSE 0 END) AS used
        FROM seats s
        LEFT JOIN passengers pp ON pp.seat_id = s.seat_id AND pp.seat_status='RAC'
        LEFT JOIN bookings bb ON bb.pnr_id = pp.pnr_id AND bb.journey_date=%s
            AND NOT (bb.to_seq <= %s OR bb.from_seq >= %s)
        WHERE s.tcq_id=%s AND s.is_rac_seat=1
        GROUP BY s.seat_id
        """,
        (journey_date, from_seq, to_seq, tcq_id),
    )
    rac_rows = cur.fetchall()
    rac_available = sum(1 for r in rac_rows if (r["used"] or 0) < 2)

    cur.execute(
        """
        SELECT COUNT(*) AS n FROM passengers pp
        JOIN bookings bb ON bb.pnr_id = pp.pnr_id
        WHERE pp.seat_status='WAITLIST' AND bb.status='ACTIVE'
          AND bb.journey_date=%s
          AND bb.train_id=(SELECT train_id FROM train_class_quota WHERE tcq_id=%s)
          AND bb.class_id=(SELECT class_id FROM train_class_quota WHERE tcq_id=%s)
          AND bb.quota_id=(SELECT quota_id FROM train_class_quota WHERE tcq_id=%s)
        """,
        (journey_date, tcq_id, tcq_id, tcq_id),
    )
    wl_len = cur.fetchone()["n"]
    return available, rac_available, wl_len


# Simulated demand: on the real app, other people grab Tatkal seats as soon as
# it opens. We only have this app's own bookings, so a pool also "sells out"
# gradually after opening: each train+class+date gets a fixed sell-out time
# (20 minutes to ~23 hours after opening) derived from its ids, and the seats
# held for other buyers grow linearly until then. Set to False to turn it off.
TATKAL_SIMULATED_DEMAND = True


def tatkal_simulated_held(train_id, class_code, journey_date, total_seats, opens_at):
    if not TATKAL_SIMULATED_DEMAND:
        return 0
    minutes = (datetime.now() - opens_at).total_seconds() / 60
    if minutes <= 0:
        return 0
    sell_out = 20 + zlib.crc32(f"{train_id}|{class_code}|{journey_date}".encode()) % 1400
    return min(total_seats, int(total_seats * min(1.0, minutes / sell_out) + 0.999))


def quota_window_status(quota, journey_date):
    """Returns (is_open, opens_at_datetime). PyMySQL returns TIME columns as timedelta."""
    open_date = journey_date - timedelta(days=quota["opens_days_before"])
    open_time = quota["opens_at_time"]  # timedelta or None
    opens_at = datetime.combine(open_date, datetime.min.time())
    if open_time is not None:
        opens_at += open_time
    is_open = datetime.now() >= opens_at
    return is_open, opens_at


@app.route("/search")
def search():
    from_city_id = request.args.get("from_city_id", type=int)
    to_city_id = request.args.get("to_city_id", type=int)
    journey_date_str = request.args.get("journey_date")
    journey_date = datetime.strptime(journey_date_str, "%Y-%m-%d").date()

    all_classes = CLASSES + PREMIUM_CLASSES
    sel_classes = [c for c in request.args.getlist("cls") if c in all_classes]
    sel_slots = [s for s in request.args.getlist("slot") if s in DEPARTURE_SLOTS]
    max_price = request.args.get("max_price", type=int)
    hide_wl = request.args.get("hide_wl") == "1"
    sort = request.args.get("sort", "")
    if sort not in SORT_SQL:
        sort = ""
    filters_active = bool(sel_classes or sel_slots or max_price or hide_wl or sort)
    show_tatkal = (journey_date - date.today()).days <= 1

    # SQL fragments below come from fixed whitelists (never raw user input);
    # user values (cities, classes, max fare) are bound as %s parameters.
    slot_sql = ""
    if sel_slots:
        slot_sql = " AND (" + " OR ".join(DEPARTURE_SLOTS[s][1] for s in sel_slots) + ")"
    quota_sql = "'GN','TQ'" if show_tatkal else "'GN'"
    class_codes = sel_classes or all_classes
    class_marks = ",".join(["%s"] * len(class_codes))
    price_sql = " AND o.fare <= %s" if max_price else ""
    order_sql = SORT_SQL[sort] if sort else "trip.train_id"
    params = [from_city_id, to_city_id, *class_codes] + ([max_price] if max_price else [])

    db = get_db()
    with db.cursor() as cur:
        # One query does the whole search: city-grouped trip finding (CTEs +
        # window functions), the departure-time filter, fare calculation per
        # class/quota, the class and max-fare filters, and the ordering.
        cur.execute(
            f"""
            WITH from_matches AS (
                SELECT r.train_id, r.seq_no AS from_seq, r.distance_km AS from_km,
                       r.station_id AS from_station_id,
                       r.departure_time AS dep_time, r.departure_day AS dep_day,
                       ROW_NUMBER() OVER (PARTITION BY r.train_id ORDER BY r.seq_no ASC) AS rn
                FROM routes r JOIN stations s ON s.station_id = r.station_id
                WHERE s.city_id = %s
            ),
            to_matches AS (
                SELECT r.train_id, r.seq_no AS to_seq, r.distance_km AS to_km,
                       r.station_id AS to_station_id,
                       r.arrival_time AS arr_time, r.arrival_day AS arr_day,
                       ROW_NUMBER() OVER (PARTITION BY r.train_id ORDER BY r.seq_no DESC) AS rn
                FROM routes r JOIN stations s ON s.station_id = r.station_id
                WHERE s.city_id = %s
            ),
            trip AS (
                SELECT t.train_id, t.number, t.name,
                       fm.from_seq, fm.from_km, fm.from_station_id, fm.dep_time, fm.dep_day,
                       tm.to_seq, tm.to_km, tm.to_station_id, tm.arr_time, tm.arr_day,
                       tm.to_km - fm.from_km AS km,
                       (t.name LIKE '%%Tejas%%' OR t.name LIKE '%%Vande Bharat%%') AS is_premium
                FROM trains t
                JOIN from_matches fm ON fm.train_id = t.train_id AND fm.rn = 1
                JOIN to_matches tm ON tm.train_id = t.train_id AND tm.rn = 1
                WHERE fm.from_seq < tm.to_seq{slot_sql}
            ),
            fare_options AS (
                SELECT trip.train_id, c.code AS class_code, q.code AS quota_code, q.name AS quota_name,
                       q.opens_days_before, q.opens_at_time, tcq.tcq_id, tcq.total_seats,
                       ROUND(GREATEST(
                           (c.base_fare + trip.km * c.fare_per_km) * q.fare_multiplier + c.reservation_charge,
                           IF(trip.is_premium, {PREMIUM_MIN_FARE}, 0)), 2) AS fare
                FROM trip
                JOIN classes c ON trip.is_premium = (c.code IN ('CC','EC'))
                JOIN quotas q ON q.code IN ({quota_sql})
                             AND NOT (trip.is_premium AND q.code = 'TQ')   -- Tejas / Vande Bharat have no Tatkal
                JOIN train_class_quota tcq ON tcq.train_id = trip.train_id
                                          AND tcq.class_id = c.class_id AND tcq.quota_id = q.quota_id
                WHERE c.code IN ({class_marks})
            )
            SELECT trip.*, o.class_code, o.quota_code, o.quota_name,
                   o.opens_days_before, o.opens_at_time, o.tcq_id, o.total_seats, o.fare,
                   MIN(o.fare) OVER (PARTITION BY trip.train_id) AS cheapest
            FROM trip JOIN fare_options o ON o.train_id = trip.train_id
            WHERE 1 = 1{price_sql}
            ORDER BY {order_sql}, trip.train_id,
                     FIELD(o.class_code, 'SL','3A','2A','CC','EC'), FIELD(o.quota_code, 'GN','TQ')
            """,
            params,
        )
        rows = cur.fetchall()

        # Group the flat rows back into trains; seat availability stays a
        # per-option query (it depends on the journey date and existing bookings).
        trains, by_id = [], {}
        for r in rows:
            t = by_id.get(r["train_id"])
            if t is None:
                t = {k: r[k] for k in ("train_id", "number", "name", "from_seq", "from_km", "from_station_id",
                                       "dep_time", "dep_day", "to_seq", "to_km", "to_station_id",
                                       "arr_time", "arr_day")}
                t["distance_km"] = r["km"]
                dep, arr = format_hhmm(t["dep_time"]), format_hhmm(t["arr_time"])
                if dep and arr:
                    day_note = f" (+{t['arr_day'] - t['dep_day']}d)" if t["arr_day"] and t["dep_day"] and t["arr_day"] > t["dep_day"] else ""
                    t["timing"] = f"Dep {dep} → Arr {arr}{day_note}"
                else:
                    t["timing"] = None
                t["options"] = []
                by_id[t["train_id"]] = t
                trains.append(t)
            is_open, opens_at = quota_window_status(r, journey_date)
            avail, rac_avail, wl_len = seat_availability(
                cur, r["tcq_id"], journey_date, t["from_seq"], t["to_seq"]
            )
            # Tatkal never has RAC or a waitlist: once it is sold out, or on the
            # day of the journey (Tatkal closes the day before), it shows REGRET.
            is_tatkal = r["quota_code"] == "TQ"
            if is_tatkal and is_open:
                held = tatkal_simulated_held(r["train_id"], r["class_code"], journey_date_str,
                                             r["total_seats"], opens_at)
                avail = max(0, avail - held)
            regret =is_tatkal and (journey_date <= date.today() or (is_open and avail <= 0))
            if regret:
                status = "REGRET"
            elif avail > 0:
                status = f"AVAILABLE-{avail}"
            elif rac_avail > 0:
                status = f"RAC {rac_avail} left"
            else:
                status = f"WL {wl_len + 1}"
            if hide_wl and (status.startswith("WL") or regret):
                continue
            t["options"].append({
                "class_code": r["class_code"],
                "quota_code": r["quota_code"],
                "quota_name": r["quota_name"],
                "status": status,
                "regret": regret,
                "is_open": is_open and not regret,
                "opens_at": opens_at.strftime("%d %b, %I:%M %p"),
                "fare": float(r["fare"]),
            })
        if hide_wl:
            trains = [t for t in trains if t["options"]]

        if trains:
            station_ids = {t["from_station_id"] for t in trains} | {t["to_station_id"] for t in trains}
            cur.execute(
                f"SELECT station_id, code, name FROM stations WHERE station_id IN ({','.join(['%s']*len(station_ids))})",
                tuple(station_ids),
            )
            station_lookup = {s["station_id"]: s for s in cur.fetchall()}
            for t in trains:
                t["from_id"] = t["from_station_id"]
                t["to_id"] = t["to_station_id"]
                t["from_station"] = station_lookup[t["from_station_id"]]
                t["to_station"] = station_lookup[t["to_station_id"]]

    return render_template(
        "app.html", page="search", trains=trains,
        from_city_id=from_city_id, to_city_id=to_city_id,
        journey_date=journey_date_str,
        all_classes=all_classes, slots=DEPARTURE_SLOTS,
        f_classes=sel_classes, f_slots=sel_slots, f_max_price=max_price,
        f_hide_wl=hide_wl, f_sort=sort, filters_active=filters_active,
        filters_count=len(sel_classes) + len(sel_slots) + bool(max_price) + hide_wl + bool(sort),
    )


# -------------------------------------------------------------- booking ----

@app.route("/book/<int:train_id>/<class_code>/<quota_code>")
@login_required
def book_form(train_id, class_code, quota_code):
    from_id = request.args.get("from_id", type=int)
    to_id = request.args.get("to_id", type=int)
    journey_date = request.args.get("journey_date")

    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM trains WHERE train_id=%s", (train_id,))
        train = cur.fetchone()
        cur.execute("SELECT * FROM stations WHERE station_id IN (%s,%s)", (from_id, to_id))
        stations = {s["station_id"]: s for s in cur.fetchall()}
        cur.execute(
            "SELECT station_id, seq_no FROM routes WHERE train_id=%s AND station_id IN (%s,%s)",
            (train_id, from_id, to_id),
        )
        seqs = {r["station_id"]: r["seq_no"] for r in cur.fetchall()}

    if from_id not in stations or to_id not in stations or from_id not in seqs or to_id not in seqs \
            or seqs[from_id] >= seqs[to_id]:
        flash("Invalid journey: the destination must come after the boarding station on this train's route.", "danger")
        return redirect(url_for("index"))

    return render_template(
        "app.html", page="book", train=train, class_code=class_code, quota_code=quota_code,
        from_id=from_id, to_id=to_id, journey_date=journey_date,
        from_station=stations[from_id], to_station=stations[to_id],
        has_berths=class_code not in PREMIUM_CLASSES,
    )


@app.route("/book", methods=["POST"])
@login_required
def book_submit():
    train_id = request.form.get("train_id", type=int)
    class_code = request.form["class_code"]
    quota_code = request.form["quota_code"]
    from_id = request.form.get("from_id", type=int)
    to_id = request.form.get("to_id", type=int)
    journey_date = request.form["journey_date"]
    names = request.form.getlist("name[]")
    ages = request.form.getlist("age[]")
    genders = request.form.getlist("gender[]")
    berth_prefs = request.form.getlist("berth_pref[]")
    food_prefs = request.form.getlist("food_pref[]")

    # Server-side checks: the browser form can be bypassed with a hand-made request.
    pax = list(zip(names, ages, genders, berth_prefs, food_prefs))
    pax = [p for p in pax if p[0].strip()]
    valid = (
        1 <= len(pax) <= 6
        and all(len(p[0].strip()) <= 100 and p[1].isdigit() and 0 < int(p[1]) <= 120
                and p[2] in ("M", "F", "O") for p in pax)
        and len(names) == len(ages) == len(genders) == len(berth_prefs) == len(food_prefs)
    )
    if not valid:
        flash("Enter 1 to 6 passengers, each with a name, an age (1-120) and a gender.", "danger")
        return redirect(request.referrer or url_for("index"))

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT name FROM trains WHERE train_id=%s", (train_id,))
            train_name = cur.fetchone()["name"]
            cur.execute("SELECT class_id, fare_per_km, base_fare, reservation_charge FROM classes WHERE code=%s", (class_code,))
            cls = cur.fetchone()
            cur.execute("SELECT quota_id, fare_multiplier FROM quotas WHERE code=%s", (quota_code,))
            quota = cur.fetchone()
            cur.execute(
                "SELECT seq_no, distance_km FROM routes WHERE train_id=%s AND station_id=%s",
                (train_id, from_id),
            )
            r1 = cur.fetchone()
            cur.execute(
                "SELECT seq_no, distance_km FROM routes WHERE train_id=%s AND station_id=%s",
                (train_id, to_id),
            )
            r2 = cur.fetchone()
            # The boarding station must come before the destination on this train's route.
            if not r1 or not r2 or r1["seq_no"] >= r2["seq_no"]:
                flash("Invalid journey: the destination must come after the boarding station on this train's route.", "danger")
                return redirect(url_for("index"))
            distance = r2["distance_km"] - r1["distance_km"]

            # dynamic surge based on current confirmed occupancy for this class+quota+date
            cur.execute(
                "SELECT tcq_id, total_seats FROM train_class_quota WHERE train_id=%s AND class_id=%s AND quota_id=%s",
                (train_id, cls["class_id"], quota["quota_id"]),
            )
            tcq = cur.fetchone()
            if quota_code == "TQ" and is_premium_train(train_name):
                db.rollback()
                flash("Tatkal is not available on Tejas / Vande Bharat trains.", "danger")
                return redirect(url_for("index"))
            if tcq is None:      # this train has no seat pool for that class / quota
                db.rollback()
                flash("That class or quota is not offered on this train.", "danger")
                return redirect(url_for("index"))
            if quota_code == "TQ" and datetime.strptime(journey_date, "%Y-%m-%d").date() <= date.today():
                db.rollback()
                flash("REGRET: Tatkal is closed on the day of the journey.", "danger")
                return redirect(url_for("index"))
            cur.execute(
                """
                SELECT COUNT(*) AS n FROM passengers pp
                JOIN bookings bb ON bb.pnr_id=pp.pnr_id
                WHERE pp.seat_status='CONFIRMED' AND bb.status='ACTIVE'
                  AND bb.train_id=%s AND bb.class_id=%s AND bb.quota_id=%s AND bb.journey_date=%s
                """,
                (train_id, cls["class_id"], quota["quota_id"], journey_date),
            )
            confirmed_now = cur.fetchone()["n"]
            occ_pct = confirmed_now / tcq["total_seats"] * 100
            surge = 1.0
            if occ_pct >= 95:
                surge = 1.3
            elif occ_pct >= 80:
                surge = 1.2
            elif occ_pct >= 50:
                surge = 1.1

            # Tatkal: same availability the search page showed (real bookings plus
            # the simulated other buyers), and no waitlist if it is not enough.
            if quota_code == "TQ":
                cur.execute("SELECT opens_days_before, opens_at_time FROM quotas WHERE quota_id=%s", (quota["quota_id"],))
                q_row = cur.fetchone()
                _, opens_at = quota_window_status(q_row, datetime.strptime(journey_date, "%Y-%m-%d").date())
                free, _, _ = seat_availability(cur, tcq["tcq_id"], datetime.strptime(journey_date, "%Y-%m-%d").date(),
                                               r1["seq_no"], r2["seq_no"])
                held = tatkal_simulated_held(train_id, class_code, journey_date, tcq["total_seats"], opens_at)
                if max(0, free - held) < len(pax):
                    db.rollback()
                    flash("REGRET: Tatkal tickets for this train and class are sold out. "
                          "Tatkal has no waiting list.", "danger")
                    return redirect(url_for("index"))

            fare_per_pax = compute_fare(distance, cls, float(quota["fare_multiplier"]), surge, is_premium=is_premium_train(train_name))

            # PNR: a random, unguessable 10-digit number (first digit never 0),
            # not the table's sequential auto-increment. Retry on the rare clash.
            for _ in range(10):
                pnr_id = 1_000_000_000 + secrets.randbelow(9_000_000_000)
                try:
                    cur.execute(
                        """
                        INSERT INTO bookings (pnr_id, user_id, train_id, class_id, quota_id, journey_date,
                                               from_station_id, to_station_id, from_seq, to_seq, total_fare)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (pnr_id, session["user_id"], train_id, cls["class_id"], quota["quota_id"], journey_date,
                         from_id, to_id, r1["seq_no"], r2["seq_no"], 0),
                    )
                    break
                except pymysql.err.IntegrityError as e:
                    if e.args[0] != 1062:     # only "duplicate PNR" is retried
                        raise
            else:
                raise RuntimeError("could not allocate a PNR")

            for name, age, gender, berth_pref, food_pref in zip(names, ages, genders, berth_prefs, food_prefs):
                if not name.strip():
                    continue
                cur.execute(
                    "CALL sp_add_passenger(%s,%s,%s,%s,%s,%s,@pid,@status)",
                    (pnr_id, name.strip(), int(age), gender, berth_pref, food_pref),
                )
                cur.execute("SELECT @pid AS pid, @status AS status")
                got = cur.fetchone()
                # Tatkal has no RAC / waitlist: if this passenger did not get a
                # confirmed seat, undo the whole booking and show REGRET.
                if quota_code == "TQ" and got["status"] != "CONFIRMED":
                    db.rollback()
                    flash("REGRET: Tatkal tickets for this train and class are sold out. "
                          "Tatkal has no waiting list.", "danger")
                    return redirect(url_for("index"))

            total_fare = fare_per_pax * len([n for n in names if n.strip()])
            cur.execute("UPDATE bookings SET total_fare=%s WHERE pnr_id=%s", (total_fare, pnr_id))
            cur.execute(
                "INSERT INTO payments (pnr_id, amount, status) VALUES (%s,%s,'SUCCESS')",
                (pnr_id, total_fare),
            )
        db.commit()
    except pymysql.err.OperationalError as e:
        db.rollback()
        flash(signal_message(e), "danger")
        return redirect(url_for("index"))

    return redirect(url_for("pnr_detail", pnr_id=pnr_id))


# ------------------------------------------------------------- PNR / cx ----

@app.route("/pnr-status", methods=["GET", "POST"])
def pnr_status():
    if request.method == "POST":
        pnr_id = request.form["pnr"].strip().lstrip("0") or "0"
        if not pnr_id.isdigit() or len(pnr_id) > 12:
            flash("Enter a valid PNR number.", "danger")
            return redirect(url_for("pnr_status"))
        return redirect(url_for("pnr_detail", pnr_id=int(pnr_id)))
    return render_template("app.html", page="pnr_status")


@app.route("/pnr/<int:pnr_id>")
@login_required
def pnr_detail(pnr_id):
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT b.*, t.number AS train_number, t.name AS train_name,
                   c.code AS class_code, q.code AS quota_code,
                   fs.name AS from_name, ts.name AS to_name,
                   r1.departure_time AS dep_time, r1.departure_day AS dep_day,
                   r2.arrival_time AS arr_time, r2.arrival_day AS arr_day
            FROM bookings b
            JOIN trains t ON t.train_id=b.train_id
            JOIN classes c ON c.class_id=b.class_id
            JOIN quotas q ON q.quota_id=b.quota_id
            JOIN stations fs ON fs.station_id=b.from_station_id
            JOIN stations ts ON ts.station_id=b.to_station_id
            JOIN routes r1 ON r1.train_id=b.train_id AND r1.station_id=b.from_station_id
            JOIN routes r2 ON r2.train_id=b.train_id AND r2.station_id=b.to_station_id
            WHERE b.pnr_id=%s
            """,
            (pnr_id,),
        )
        booking = cur.fetchone()
        # Same message whether the PNR doesn't exist or belongs to someone else,
        # so PNR numbers can't be probed to see which exist.
        if not booking or (booking["user_id"] != session["user_id"] and not session.get("is_admin")):
            flash("No such PNR.", "danger")
            return redirect(url_for("pnr_status"))
        dep = format_hhmm(booking["dep_time"])
        arr = format_hhmm(booking["arr_time"])
        if dep and arr:
            day_note = f" (+{booking['arr_day'] - booking['dep_day']}d)" if booking["arr_day"] and booking["dep_day"] and booking["arr_day"] > booking["dep_day"] else ""
            booking["timing"] = f"Dep {dep} → Arr {arr}{day_note}"
        else:
            booking["timing"] = None
        cur.execute(
            "SELECT * FROM passengers WHERE pnr_id=%s ORDER BY passenger_id", (pnr_id,)
        )
        passengers = cur.fetchall()
        for p in passengers:
            if p["seat_status"] == "CONFIRMED":
                cur.execute("SELECT coach_number, seat_number, berth_type FROM seats WHERE seat_id=%s", (p["seat_id"],))
                seat = cur.fetchone()
                p["display_status"] = f"CNF / {seat['coach_number']}-{seat['seat_number']}{format_berth(seat['berth_type'])}"
            elif p["seat_status"] == "RAC":
                cur.execute("SELECT coach_number, seat_number, berth_type FROM seats WHERE seat_id=%s", (p["seat_id"],))
                seat = cur.fetchone()
                p["display_status"] = f"RAC {p['rac_slot']} / {seat['coach_number']}-{seat['seat_number']}{format_berth(seat['berth_type'])}"
            elif p["seat_status"] == "WAITLIST":
                p["display_status"] = f"WL {p['wl_number']}"
            else:
                p["display_status"] = "CANCELLED"

    return render_template("app.html", page="pnr_detail", booking=booking, passengers=passengers,
                            pnr_display=f"{pnr_id:010d}")


@app.route("/my-bookings")
@login_required
def my_bookings():
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT b.pnr_id, b.journey_date, b.status, b.total_fare,
                   t.number AS train_number, t.name AS train_name,
                   fs.name AS from_name, ts.name AS to_name
            FROM bookings b
            JOIN trains t ON t.train_id=b.train_id
            JOIN stations fs ON fs.station_id=b.from_station_id
            JOIN stations ts ON ts.station_id=b.to_station_id
            WHERE b.user_id=%s ORDER BY b.booking_time DESC
            """,
            (session["user_id"],),
        )
        bookings = cur.fetchall()
    return render_template("app.html", page="my_bookings", bookings=bookings)


@app.route("/cancel/<int:passenger_id>", methods=["POST"])
@login_required
def cancel_passenger(passenger_id):
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT b.user_id, b.pnr_id, p.name FROM passengers p JOIN bookings b ON b.pnr_id=p.pnr_id WHERE p.passenger_id=%s",
            (passenger_id,),
        )
        row = cur.fetchone()
        if not row or row["user_id"] != session["user_id"]:
            flash("Not your booking.", "danger")
            return redirect(url_for("my_bookings"))
        cur.execute("CALL sp_cancel_passenger(%s)", (passenger_id,))
        cur.execute("SELECT status FROM bookings WHERE pnr_id=%s", (row["pnr_id"],))
        whole_ticket = cur.fetchone()["status"] == "CANCELLED"
    db.commit()
    if whole_ticket:
        flash("Ticket cancelled.", "info")
    else:
        flash(f"Ticket cancelled for {row['name']}. The other passengers on this PNR are still booked.", "info")
    return redirect(url_for("pnr_detail", pnr_id=row["pnr_id"]))


# ------------------------------------------------------------ dashboard ----

@app.route("/dashboard")
@admin_required
def dashboard():
    return render_template("app.html", page="dashboard")


@app.route("/api/dashboard-data")
@admin_required
def dashboard_data():
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM vw_train_occupancy ORDER BY journey_date, number")
        occupancy = cur.fetchall()
        cur.execute("SELECT * FROM vw_revenue_by_train ORDER BY total_revenue DESC")
        revenue = cur.fetchall()
    for row in occupancy:
        row["journey_date"] = row["journey_date"].isoformat()
    for row in revenue:
        row["total_revenue"] = float(row["total_revenue"] or 0)
    return jsonify({"occupancy": occupancy, "revenue": revenue})


if __name__ == "__main__":
    app.run(debug=DEBUG, port=5001)
