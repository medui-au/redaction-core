-- Practice locations — the strings that identify this practice's sites.
--
-- Multi-site practices have several rows, and BP keeps historical ones with
-- RECORDSTATUS = 0. All rows are returned, active or not: an old address or
-- number still appears in old correspondence, and redacting a former site's
-- details costs nothing.
--
-- Rows duplicate each other heavily (the same address recorded under several
-- location codes), so the caller de-duplicates. LOCATIONNAME is read by
-- practice_name.sql instead (ADR-121).
SELECT
    L.LOCATIONID                        AS LOCATIONID,
    L.RECORDSTATUS                      AS RECORDSTATUS,
    TRIM(ISNULL(L.ADDRESS1, ''))        AS ADDRESS1,
    TRIM(ISNULL(L.ADDRESS2, ''))        AS ADDRESS2,
    TRIM(ISNULL(L.CITY, ''))            AS CITY,
    TRIM(ISNULL(L.POSTCODE, ''))        AS POSTCODE,
    TRIM(ISNULL(L.PHONE, ''))           AS PHONE,
    TRIM(ISNULL(L.AHPHONE, ''))         AS AHPHONE,
    TRIM(ISNULL(L.FAX, ''))             AS FAX,
    TRIM(ISNULL(L.EMAIL, ''))           AS EMAIL
FROM LOCATIONS AS L
WHERE LEN(TRIM(ISNULL(L.ADDRESS1, ''))) > 0
   OR LEN(TRIM(ISNULL(L.PHONE, ''))) > 0
   OR LEN(TRIM(ISNULL(L.FAX, ''))) > 0
   OR LEN(TRIM(ISNULL(L.EMAIL, ''))) > 0
