-- The practice's own doctors (BP GroupCode 3 principal, 4 employee, 42 contract),
-- current and former, for the HCP pass. Doctors not in the address book were
-- never redacted: their letters went out as "Dr J. Smith" in plain view.
-- Deliberately no status filter (USERSTATUS / RECORDSTATUS): inactive and deleted users
-- still count. That includes "shadow" users a practice enters under a practitioner's
-- preferred name, so tools that read only BP redact and detect it too (MedUI ADR-112).
-- Do not add an active-only filter.
SELECT UserId            AS user_id,
       TRIM(FirstName)   AS first_name,
       TRIM(Surname)     AS surname
FROM USERS
WHERE GroupCode IN (3, 4, 42)
  AND Surname IS NOT NULL AND LEN(TRIM(Surname)) > 1;
