-- ============================================================
-- Railway Reservation System - Stored Procedures
-- Booking + cancellation logic, including the RAC/Waitlist cascade.
-- ============================================================

USE railway_reservation;

DELIMITER $$

-- ------------------------------------------------------------
-- sp_add_passenger
-- Assigns one passenger to an existing PNR: tries a confirmed seat
-- (no segment overlap with existing confirmed bookings), then an
-- RAC slot, then falls back to the waitlist queue.
-- Uses SELECT ... FOR UPDATE to make concurrent bookings safe.
-- ------------------------------------------------------------
CREATE PROCEDURE sp_add_passenger(
    IN p_pnr_id     BIGINT,
    IN p_name       VARCHAR(100),
    IN p_age        INT,
    IN p_gender     CHAR(1),
    IN p_berth_pref VARCHAR(20),
    IN p_food_pref  VARCHAR(10),
    OUT p_passenger_id INT,
    OUT p_status       VARCHAR(20)
)
proc_body: BEGIN
    DECLARE v_train_id, v_class_id, v_quota_id INT;
    DECLARE v_from_seq, v_to_seq INT;
    DECLARE v_journey_date DATE;
    DECLARE v_tcq_id INT;
    DECLARE v_seat_id INT DEFAULT NULL;
    DECLARE v_rac_seat_id INT DEFAULT NULL;
    DECLARE v_rac_used INT;
    DECLARE v_next_wl INT;

    SELECT train_id, class_id, quota_id, from_seq, to_seq, journey_date
      INTO v_train_id, v_class_id, v_quota_id, v_from_seq, v_to_seq, v_journey_date
      FROM bookings WHERE pnr_id = p_pnr_id;

    SELECT tcq_id INTO v_tcq_id FROM train_class_quota
      WHERE train_id = v_train_id AND class_id = v_class_id AND quota_id = v_quota_id;

    -- 1) Try a free non-RAC seat: no existing CONFIRMED passenger on that
    --    seat whose journey segment overlaps [from_seq, to_seq) on this date.
    --    Prefer a seat matching the requested berth (best-effort, like real
    --    IRCTC: a preference, not a guarantee), then fall back to any seat.
    SELECT s.seat_id INTO v_seat_id
    FROM seats s
    WHERE s.tcq_id = v_tcq_id AND s.is_rac_seat = 0
      AND NOT EXISTS (
            SELECT 1 FROM passengers pp
            JOIN bookings bb ON bb.pnr_id = pp.pnr_id
            WHERE pp.seat_id = s.seat_id
              AND pp.seat_status = 'CONFIRMED'
              AND bb.journey_date = v_journey_date
              AND NOT (bb.to_seq <= v_from_seq OR bb.from_seq >= v_to_seq)
      )
    ORDER BY (s.berth_type = p_berth_pref) DESC, s.seat_id
    LIMIT 1
    FOR UPDATE;

    IF v_seat_id IS NOT NULL THEN
        INSERT INTO passengers (pnr_id, name, age, gender, berth_preference, food_preference, seat_status, seat_id)
        VALUES (p_pnr_id, p_name, p_age, p_gender, p_berth_pref, p_food_pref, 'CONFIRMED', v_seat_id);
        SET p_passenger_id = LAST_INSERT_ID();
        SET p_status = 'CONFIRMED';
        LEAVE proc_body;
    END IF;

    -- 2) Try an RAC seat with fewer than 2 overlapping RAC occupants.
    SELECT s.seat_id, COUNT(pp.passenger_id) INTO v_rac_seat_id, v_rac_used
    FROM seats s
    LEFT JOIN passengers pp ON pp.seat_id = s.seat_id AND pp.seat_status = 'RAC'
    LEFT JOIN bookings bb ON bb.pnr_id = pp.pnr_id
        AND bb.journey_date = v_journey_date
        AND NOT (bb.to_seq <= v_from_seq OR bb.from_seq >= v_to_seq)
    WHERE s.tcq_id = v_tcq_id AND s.is_rac_seat = 1
    GROUP BY s.seat_id
    HAVING COUNT(bb.pnr_id) < 2
    ORDER BY s.seat_id
    LIMIT 1
    FOR UPDATE;

    IF v_rac_seat_id IS NOT NULL THEN
        INSERT INTO passengers (pnr_id, name, age, gender, berth_preference, food_preference, seat_status, seat_id, rac_slot)
        VALUES (p_pnr_id, p_name, p_age, p_gender, p_berth_pref, p_food_pref, 'RAC', v_rac_seat_id,
                IFNULL(v_rac_used, 0) + 1);
        SET p_passenger_id = LAST_INSERT_ID();
        SET p_status = 'RAC';
        LEAVE proc_body;
    END IF;

    -- 3) Waitlist: next number in line for this train/class/quota/date.
    SELECT IFNULL(MAX(pp.wl_number), 0) + 1 INTO v_next_wl
    FROM passengers pp
    JOIN bookings bb ON bb.pnr_id = pp.pnr_id
    WHERE pp.seat_status = 'WAITLIST'
      AND bb.train_id = v_train_id AND bb.class_id = v_class_id
      AND bb.quota_id = v_quota_id AND bb.journey_date = v_journey_date
      AND bb.status = 'ACTIVE';

    INSERT INTO passengers (pnr_id, name, age, gender, berth_preference, food_preference, seat_status, wl_number)
    VALUES (p_pnr_id, p_name, p_age, p_gender, p_berth_pref, p_food_pref, 'WAITLIST', v_next_wl);
    SET p_passenger_id = LAST_INSERT_ID();
    SET p_status = CONCAT('WL', v_next_wl);
END$$

-- ------------------------------------------------------------
-- sp_cancel_passenger
-- Cancels one passenger and cascades the promotion:
--   freed CONFIRMED seat  -> earliest RAC passenger promoted to CONFIRMED
--   freed RAC slot        -> earliest WAITLIST passenger promoted to RAC
--   any WAITLIST cancel   -> queue behind it shifts up by one
-- ------------------------------------------------------------
CREATE PROCEDURE sp_cancel_passenger(
    IN p_passenger_id INT
)
proc_body: BEGIN
    DECLARE v_pnr_id BIGINT;
    DECLARE v_old_status VARCHAR(20);
    DECLARE v_seat_id INT;
    DECLARE v_train_id, v_class_id, v_quota_id INT;
    DECLARE v_journey_date DATE;
    DECLARE v_from_seq, v_to_seq INT;
    DECLARE v_promote_passenger_id INT;
    DECLARE v_promote_wl INT;

    SELECT pp.pnr_id, pp.seat_status, pp.seat_id
      INTO v_pnr_id, v_old_status, v_seat_id
      FROM passengers pp WHERE pp.passenger_id = p_passenger_id;

    SELECT train_id, class_id, quota_id, journey_date, from_seq, to_seq
      INTO v_train_id, v_class_id, v_quota_id, v_journey_date, v_from_seq, v_to_seq
      FROM bookings WHERE pnr_id = v_pnr_id;

    UPDATE passengers
       SET seat_status = 'CANCELLED', seat_id = NULL, rac_slot = NULL, wl_number = NULL
     WHERE passenger_id = p_passenger_id;

    IF v_old_status = 'CONFIRMED' THEN
        -- promote earliest RAC passenger on ANY rac seat for this train/class/quota/date to this freed seat
        SELECT pp.passenger_id INTO v_promote_passenger_id
        FROM passengers pp
        JOIN bookings bb ON bb.pnr_id = pp.pnr_id
        WHERE pp.seat_status = 'RAC'
          AND bb.train_id = v_train_id AND bb.class_id = v_class_id
          AND bb.quota_id = v_quota_id AND bb.journey_date = v_journey_date
          AND bb.status = 'ACTIVE'
        ORDER BY pp.passenger_id
        LIMIT 1 FOR UPDATE;

        IF v_promote_passenger_id IS NOT NULL THEN
            UPDATE passengers
               SET seat_status = 'CONFIRMED', seat_id = v_seat_id, rac_slot = NULL
             WHERE passenger_id = v_promote_passenger_id;
        END IF;
    END IF;

    IF v_old_status IN ('CONFIRMED','RAC') THEN
        -- an RAC slot is now open (either just vacated, or freed by the promotion above) -> pull WL1 into RAC
        SELECT pp.passenger_id INTO v_promote_passenger_id
        FROM passengers pp
        JOIN bookings bb ON bb.pnr_id = pp.pnr_id
        WHERE pp.seat_status = 'WAITLIST'
          AND bb.train_id = v_train_id AND bb.class_id = v_class_id
          AND bb.quota_id = v_quota_id AND bb.journey_date = v_journey_date
          AND bb.status = 'ACTIVE'
        ORDER BY pp.wl_number
        LIMIT 1 FOR UPDATE;

        IF v_promote_passenger_id IS NOT NULL THEN
            IF v_old_status = 'RAC' THEN
                UPDATE passengers SET seat_status = 'RAC', seat_id = v_seat_id, rac_slot = 1, wl_number = NULL
                 WHERE passenger_id = v_promote_passenger_id;
            ELSE
                SELECT s.seat_id INTO v_seat_id FROM seats s
                LEFT JOIN passengers pp2 ON pp2.seat_id = s.seat_id AND pp2.seat_status = 'RAC'
                WHERE s.tcq_id = (SELECT tcq_id FROM train_class_quota
                                   WHERE train_id = v_train_id AND class_id = v_class_id AND quota_id = v_quota_id)
                  AND s.is_rac_seat = 1
                GROUP BY s.seat_id HAVING COUNT(pp2.passenger_id) < 2
                ORDER BY s.seat_id LIMIT 1;

                UPDATE passengers SET seat_status = 'RAC', seat_id = v_seat_id, rac_slot = 1, wl_number = NULL
                 WHERE passenger_id = v_promote_passenger_id;
            END IF;

            -- shift the rest of the waitlist queue up by one
            UPDATE passengers pp
            JOIN bookings bb ON bb.pnr_id = pp.pnr_id
            SET pp.wl_number = pp.wl_number - 1
            WHERE pp.seat_status = 'WAITLIST'
              AND bb.train_id = v_train_id AND bb.class_id = v_class_id
              AND bb.quota_id = v_quota_id AND bb.journey_date = v_journey_date
              AND bb.status = 'ACTIVE';
        END IF;
    END IF;

    IF v_old_status = 'WAITLIST' THEN
        -- no promotion needed, just close the gap in the queue
        UPDATE passengers pp
        JOIN bookings bb ON bb.pnr_id = pp.pnr_id
        SET pp.wl_number = pp.wl_number - 1
        WHERE pp.seat_status = 'WAITLIST'
          AND bb.train_id = v_train_id AND bb.class_id = v_class_id
          AND bb.quota_id = v_quota_id AND bb.journey_date = v_journey_date
          AND bb.status = 'ACTIVE';
    END IF;

    -- if every passenger on this PNR is now cancelled, close the booking itself
    IF NOT EXISTS (SELECT 1 FROM passengers WHERE pnr_id = v_pnr_id AND seat_status <> 'CANCELLED') THEN
        UPDATE bookings SET status = 'CANCELLED' WHERE pnr_id = v_pnr_id;
        UPDATE payments SET status = 'REFUNDED' WHERE pnr_id = v_pnr_id;
    END IF;
END$$

DELIMITER ;
