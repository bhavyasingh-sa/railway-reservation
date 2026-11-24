-- ============================================================
-- Railway Reservation System - Seed data (classes, quotas, seat generator)
-- Users are NOT seeded either: sign up through the app, then run
--   flask --app app make-admin you@example.com
-- Trains, stations and routes are NOT seeded here: they live in the database
-- (loaded from real data). The seat-pool statements below build seats for
-- whatever trains exist in `trains` when this file is run.
-- ============================================================

USE railway_reservation;

-- ---------- Classes ----------
-- Real Tejas/Vande Bharat trains only offer CC/EC (chair car, no berths);
-- every other train uses SL/3A/2A. See trg_booking_window_check.
INSERT INTO classes (code, name, fare_per_km, base_fare, reservation_charge) VALUES
('SL','Sleeper', 0.50, 15, 20),
('3A','AC 3 Tier', 1.50, 30, 40),
('2A','AC 2 Tier', 2.20, 40, 50),
('CC','Chair Car', 1.60, 35, 45),
('EC','Executive Chair Car', 2.50, 50, 60);

-- ---------- Quotas ----------
-- opens_days_before / opens_at_time drive the trigger's booking-window check.
INSERT INTO quotas (code, name, fare_multiplier, opens_days_before, opens_at_time) VALUES
('GN','General',      1.00, 60,  NULL),       -- opens 60 days before journey, any time of day
('TQ','Tatkal',       1.30, 1,   '10:00:00'), -- opens 1 day before journey, at 10 AM
('LD','Ladies',       1.00, 60,  NULL),
('SS','Senior Citizen',0.90, 60, NULL);

-- ---------- Seat pools: small numbers on purpose, so waitlist/RAC are easy to trigger ----------
-- For each existing train, set up SL/3A/2A classes under GN and TQ quotas.
INSERT INTO train_class_quota (train_id, class_id, quota_id, total_seats, rac_seats)
SELECT t.train_id, c.class_id, q.quota_id,
       CASE WHEN q.code='TQ' THEN 3 ELSE 8 END AS total_seats,
       CASE WHEN q.code='TQ' THEN 0 ELSE 2 END AS rac_seats
FROM trains t
JOIN classes c ON c.code IN ('SL','3A','2A')
JOIN quotas q ON q.code IN ('GN','TQ');

-- Generate physical seats for every train_class_quota row.
-- Seat numbers 1..total_seats, one coach "C1"; the LAST `rac_seats` seat numbers are RAC seats.
-- Berth type follows real coach layout: SL/3A cycle Lower/Middle/Upper,
-- 2A cycles Lower/Upper (no Middle berth in real 2A coaches); RAC seats
-- sit on what would be the Side Lower berth (shared by 2 passengers).
INSERT INTO seats (tcq_id, coach_number, seat_number, is_rac_seat, berth_type)
SELECT tcq.tcq_id, 'C1', n.num,
       CASE WHEN n.num > (tcq.total_seats - tcq.rac_seats) THEN 1 ELSE 0 END,
       CASE
           WHEN n.num > (tcq.total_seats - tcq.rac_seats) THEN 'SIDE_LOWER'
           WHEN c.code = '2A' THEN ELT(1 + MOD(n.num - 1, 2), 'LOWER', 'UPPER')
           ELSE ELT(1 + MOD(n.num - 1, 3), 'LOWER', 'MIDDLE', 'UPPER')
       END
FROM train_class_quota tcq
JOIN classes c ON c.class_id = tcq.class_id
JOIN (
    SELECT 1 AS num UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5
    UNION SELECT 6 UNION SELECT 7 UNION SELECT 8
) n ON n.num <= tcq.total_seats;
