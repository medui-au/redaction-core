-- The practice's own staff other than doctors — nurses, receptionists, allied
-- health — current and former, for the HCP pass. Doctors are practice_doctors.sql;
-- GroupCode 0 (Guest) is left out: it holds vendor and integration accounts.
-- Staff not in the address book were never redacted in note text: "\nJANE
-- CITIZEN" at the end of a nurse's progress note reached the model in full.
-- Deliberately no status filter (USERSTATUS / RECORDSTATUS): inactive and deleted users
-- still count. That includes "shadow" users a practice enters under a practitioner's
-- preferred name, so tools that read only BP redact and detect it too (MedUI ADR-112).
-- Do not add an active-only filter.
SELECT u.UserId              AS user_id,
       TRIM(u.FirstName)     AS first_name,
       TRIM(u.Surname)       AS surname,
       TRIM(g.GroupName)     AS role
FROM USERS u
LEFT JOIN USERGROUPS g ON g.GroupCode = u.GroupCode
WHERE u.GroupCode NOT IN (0, 3, 4, 42)
  AND u.Surname IS NOT NULL AND LEN(TRIM(u.Surname)) > 1
  AND u.FirstName IS NOT NULL AND LEN(TRIM(u.FirstName)) > 1;
