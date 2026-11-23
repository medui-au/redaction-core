-- The practice's own BP users — doctors, nurses, receptionists, allied health —
-- current and former: the checker's tier-1 names. The HCP pass redacts the
-- same people by full name (practice_doctors.sql, ADR-037, and
-- practice_staff.sql, ADR-040).
-- GroupCode 0 (Guest) is left out: it holds vendor and integration accounts.
-- Deliberately no status filter (USERSTATUS / RECORDSTATUS): inactive and deleted users
-- still count. That includes "shadow" users a practice enters under a practitioner's
-- preferred name, so tools that read only BP redact and detect it too (MedUI ADR-112).
-- Do not add an active-only filter.
SELECT TRIM(FirstName) AS first_name,
       TRIM(Surname)   AS surname
FROM USERS
WHERE GroupCode <> 0
  AND Surname IS NOT NULL AND LEN(TRIM(Surname)) > 1
  AND FirstName IS NOT NULL AND LEN(TRIM(FirstName)) > 1;
