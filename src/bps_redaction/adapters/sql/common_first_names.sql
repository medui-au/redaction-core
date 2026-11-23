-- First names shared by at least 20 of the practice's patients (lower-cased).
-- The HCP pass uses them to spot an address-book surname that is really being
-- used as a first name ("Dr David Smith" vs a contact surnamed David).
SELECT LOWER(TRIM(FirstName)) AS name
FROM PATIENTS
WHERE FirstName IS NOT NULL AND LEN(TRIM(FirstName)) > 1
GROUP BY LOWER(TRIM(FirstName))
HAVING COUNT(*) >= 20;
