-- The practice's own name (MedUI ADR-121): as registered in BP, and each site's
-- location name. No record-status filter, as for the other practice queries
-- (ADR-112): a former name still appears in old correspondence. The caller
-- decides which rows are usable names; a one-word site label is a town, not a name.
SELECT DISTINCT 'practice' AS SOURCE, TRIM(ISNULL(P.PRACTICENAME, '')) AS NAME
FROM PRACTICE AS P
UNION
SELECT DISTINCT 'practice', TRIM(ISNULL(P.PracticeLegalName, ''))
FROM PRACTICE AS P
UNION
SELECT DISTINCT 'location', TRIM(ISNULL(L.LOCATIONNAME, ''))
FROM LOCATIONS AS L
