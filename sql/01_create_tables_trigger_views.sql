-- ============================================================
-- Railway Reservation System (IRCTC-style) - Schema
-- ============================================================

DROP DATABASE IF EXISTS railway_reservation;
CREATE DATABASE railway_reservation;
USE railway_reservation;

-- ---------- Master tables ----------

CREATE TABLE stations (
    station_id INT AUTO_INCREMENT PRIMARY KEY,
    code        VARCHAR(10) NOT NULL UNIQUE,
    name        VARCHAR(100) NOT NULL
);

CREATE TABLE trains (
    train_id    INT AUTO_INCREMENT PRIMARY KEY,
    number      VARCHAR(10) NOT NULL UNIQUE,
    name        VARCHAR(100) NOT NULL
);

-- Ordered stops for a train, with cumulative distance from origin.
CREATE TABLE routes (
    route_id        INT AUTO_INCREMENT PRIMARY KEY,
    train_id        INT NOT NULL,
    station_id      INT NOT NULL,
    seq_no          INT NOT NULL,               -- 1 = origin, increasing along the route
    distance_km     INT NOT NULL,               -- cumulative distance from origin
    arrival_time    TIME NULL,                  -- NULL at the origin stop
    arrival_day     INT NULL,                   -- day offset from origin departure (1,2,3...)
    departure_time  TIME NULL,                  -- NULL at the terminal stop
    departure_day   INT NULL,
    FOREIGN KEY (train_id) REFERENCES trains(train_id) ON DELETE CASCADE,
    FOREIGN KEY (station_id) REFERENCES stations(station_id),
    UNIQUE KEY uq_train_seq (train_id, seq_no)
);

CREATE TABLE classes (
    class_id     INT AUTO_INCREMENT PRIMARY KEY,
    code         VARCHAR(5) NOT NULL UNIQUE,     -- SL, 3A, 2A, CC, EC
    name         VARCHAR(50) NOT NULL,
    fare_per_km  DECIMAL(6,2) NOT NULL,
    base_fare    DECIMAL(6,2) NOT NULL DEFAULT 0,
    reservation_charge DECIMAL(6,2) NOT NULL DEFAULT 0
);

CREATE TABLE quotas (
    quota_id     INT AUTO_INCREMENT PRIMARY KEY,
    code         VARCHAR(10) NOT NULL UNIQUE,    -- GN, TQ, LD, SS
    name         VARCHAR(50) NOT NULL,
    fare_multiplier DECIMAL(4,2) NOT NULL DEFAULT 1.00,
    -- how many days before journey this quota opens (GN = 60, TQ = 1)
    opens_days_before INT NOT NULL,
    -- clock time the quota opens on its opening day (used by Tatkal: 10:00)
    opens_at_time TIME NULL
);

-- Seat pool configuration per train + class + quota.
CREATE TABLE train_class_quota (
    tcq_id        INT AUTO_INCREMENT PRIMARY KEY,
    train_id      INT NOT NULL,
    class_id      INT NOT NULL,
    quota_id      INT NOT NULL,
    total_seats   INT NOT NULL,
    rac_seats     INT NOT NULL DEFAULT 0,        -- how many of total_seats are RAC (2 pax/seat)
    FOREIGN KEY (train_id) REFERENCES trains(train_id) ON DELETE CASCADE,
    FOREIGN KEY (class_id) REFERENCES classes(class_id),
    FOREIGN KEY (quota_id) REFERENCES quotas(quota_id),
    UNIQUE KEY uq_train_class_quota (train_id, class_id, quota_id)
);

-- Physical seat inventory per train+class+quota.
CREATE TABLE seats (
    seat_id       INT AUTO_INCREMENT PRIMARY KEY,
    tcq_id        INT NOT NULL,
    coach_number  VARCHAR(10) NOT NULL,
    seat_number   INT NOT NULL,
    is_rac_seat   TINYINT(1) NOT NULL DEFAULT 0,
    berth_type    ENUM('LOWER','MIDDLE','UPPER','SIDE_LOWER','SIDE_UPPER') NULL,
    FOREIGN KEY (tcq_id) REFERENCES train_class_quota(tcq_id) ON DELETE CASCADE,
    UNIQUE KEY uq_seat (tcq_id, coach_number, seat_number)
);

CREATE TABLE users (
    user_id       INT AUTO_INCREMENT PRIMARY KEY,
    name          VARCHAR(100) NOT NULL,
    email         VARCHAR(120) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    is_admin      TINYINT(1) NOT NULL DEFAULT 0,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ---------- Transactional tables ----------

-- One PNR = one journey booked by one user, may carry several passengers.
CREATE TABLE bookings (
    pnr_id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id         INT NOT NULL,
    train_id        INT NOT NULL,
    class_id        INT NOT NULL,
    quota_id        INT NOT NULL,
    journey_date    DATE NOT NULL,
    from_station_id INT NOT NULL,
    to_station_id   INT NOT NULL,
    from_seq        INT NOT NULL,
    to_seq          INT NOT NULL,
    booking_time    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_fare      DECIMAL(10,2) NOT NULL DEFAULT 0,
    status          ENUM('ACTIVE','CANCELLED') NOT NULL DEFAULT 'ACTIVE',
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    FOREIGN KEY (train_id) REFERENCES trains(train_id),
    FOREIGN KEY (class_id) REFERENCES classes(class_id),
    FOREIGN KEY (quota_id) REFERENCES quotas(quota_id),
    FOREIGN KEY (from_station_id) REFERENCES stations(station_id),
    FOREIGN KEY (to_station_id) REFERENCES stations(station_id)
);

CREATE TABLE passengers (
    passenger_id  INT AUTO_INCREMENT PRIMARY KEY,
    pnr_id        BIGINT NOT NULL,
    name          VARCHAR(100) NOT NULL,
    age           INT NOT NULL,
    gender        ENUM('M','F','O') NOT NULL,
    berth_preference ENUM('NO_PREFERENCE','LOWER','MIDDLE','UPPER','SIDE_LOWER','SIDE_UPPER') NOT NULL DEFAULT 'NO_PREFERENCE',
    food_preference  ENUM('VEG','NON_VEG','NO_FOOD') NOT NULL DEFAULT 'NO_FOOD',
    seat_status   ENUM('CONFIRMED','RAC','WAITLIST','CANCELLED') NOT NULL,
    seat_id       INT NULL,                      -- set for CONFIRMED and RAC
    rac_slot      TINYINT NULL,                  -- 1 or 2, only for RAC
    wl_number     INT NULL,                      -- only for WAITLIST, 1 = first in line
    FOREIGN KEY (pnr_id) REFERENCES bookings(pnr_id) ON DELETE CASCADE,
    FOREIGN KEY (seat_id) REFERENCES seats(seat_id)
);

CREATE TABLE payments (
    payment_id     INT AUTO_INCREMENT PRIMARY KEY,
    pnr_id         BIGINT NOT NULL UNIQUE,        -- enforces 1:1 with bookings at the DB level
    amount         DECIMAL(10,2) NOT NULL,
    status         ENUM('SUCCESS','REFUNDED') NOT NULL DEFAULT 'SUCCESS',
    payment_time   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (pnr_id) REFERENCES bookings(pnr_id) ON DELETE CASCADE
);

-- ---------- Trigger: enforce Advance Reservation Period + Tatkal opening time ----------
-- A real DBMS-level business rule check: reject inserts that violate the booking window,
-- so it can never be bypassed even if the app layer has a bug.

DELIMITER $$

CREATE TRIGGER trg_booking_window_check
BEFORE INSERT ON bookings
FOR EACH ROW
BEGIN
    DECLARE v_opens_days INT;
    DECLARE v_opens_at TIME;
    DECLARE v_open_date DATE;
    DECLARE v_class_code VARCHAR(5);
    DECLARE v_train_name VARCHAR(100);
    DECLARE v_is_premium TINYINT;

    -- The destination must come after the boarding station on the route.
    IF NEW.from_seq >= NEW.to_seq THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Invalid journey: destination must come after the boarding station.';
    END IF;

    SELECT opens_days_before, opens_at_time
      INTO v_opens_days, v_opens_at
      FROM quotas WHERE quota_id = NEW.quota_id;

    IF DATEDIFF(NEW.journey_date, CURDATE()) > v_opens_days THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Booking not open yet for this quota (advance reservation period).';
    END IF;

    -- Quota opens exactly v_opens_days before journey, at v_opens_at time of day (e.g. Tatkal).
    IF v_opens_at IS NOT NULL THEN
        SET v_open_date = DATE_SUB(NEW.journey_date, INTERVAL v_opens_days DAY);
        IF CURDATE() < v_open_date
           OR (CURDATE() = v_open_date AND CURTIME() < v_opens_at) THEN
            SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'This quota has not opened yet today.';
        END IF;
    END IF;

    -- Tatkal closes the day before the journey: on the journey day itself it is "Regret".
    IF v_opens_at IS NOT NULL AND NEW.journey_date <= CURDATE() THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'REGRET: Tatkal is closed on the day of the journey.';
    END IF;

    -- Real Tejas/Vande Bharat trains only offer Chair Car (CC) / Executive
    -- Chair Car (EC); every other train uses SL/3A/2A. Enforce both ways.
    SELECT code INTO v_class_code FROM classes WHERE class_id = NEW.class_id;
    SELECT name INTO v_train_name FROM trains WHERE train_id = NEW.train_id;
    SET v_is_premium = (v_train_name LIKE '%Tejas%' OR v_train_name LIKE '%Vande Bharat%');

    IF v_is_premium AND v_class_code NOT IN ('CC','EC') THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'This train only offers Chair Car (CC) and Executive Chair Car (EC).';
    END IF;

    IF NOT v_is_premium AND v_class_code IN ('CC','EC') THEN
        SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'Chair Car classes are only available on Tejas/Vande Bharat trains.';
    END IF;
END$$

DELIMITER ;

-- ---------- Views for the admin dashboard ----------

CREATE VIEW vw_train_occupancy AS
SELECT
    t.train_id, t.number, t.name AS train_name,
    c.code AS class_code, q.code AS quota_code,
    b.journey_date,
    tcq.total_seats,
    SUM(p.seat_status = 'CONFIRMED') AS confirmed_count,
    SUM(p.seat_status = 'RAC')       AS rac_count,
    SUM(p.seat_status = 'WAITLIST')  AS waitlist_count,
    ROUND(SUM(p.seat_status = 'CONFIRMED') / tcq.total_seats * 100, 1) AS occupancy_pct
FROM bookings b
JOIN trains t   ON t.train_id = b.train_id
JOIN classes c  ON c.class_id = b.class_id
JOIN quotas q   ON q.quota_id = b.quota_id
JOIN train_class_quota tcq ON tcq.train_id = b.train_id AND tcq.class_id = b.class_id AND tcq.quota_id = b.quota_id
JOIN passengers p ON p.pnr_id = b.pnr_id
WHERE b.status = 'ACTIVE'
GROUP BY t.train_id, t.number, t.name, c.code, q.code, b.journey_date, tcq.total_seats;

CREATE VIEW vw_revenue_by_train AS
SELECT
    t.train_id, t.number, t.name AS train_name,
    COUNT(DISTINCT b.pnr_id) AS total_bookings,
    SUM(pay.amount) AS total_revenue
FROM bookings b
JOIN trains t ON t.train_id = b.train_id
JOIN payments pay ON pay.pnr_id = b.pnr_id AND pay.status = 'SUCCESS'
WHERE b.status = 'ACTIVE'
GROUP BY t.train_id, t.number, t.name;
