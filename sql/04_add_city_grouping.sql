-- ============================================================
-- City-grouped station search (RailYatri-style): search "Lucknow" and
-- match trains through ANY station in that city, not one exact station.
-- ============================================================

USE railway_reservation;

CREATE TABLE cities (
    city_id INT AUTO_INCREMENT PRIMARY KEY,
    name    VARCHAR(100) NOT NULL UNIQUE
);

ALTER TABLE stations ADD COLUMN city_id INT NULL;
ALTER TABLE stations ADD CONSTRAINT fk_stations_city
    FOREIGN KEY (city_id) REFERENCES cities(city_id);

CREATE INDEX idx_stations_city ON stations(city_id);
