-- Deliberately no record-status filter: inactive and deleted entries still count, so an
-- entry kept under a practitioner's preferred name is redacted too (MedUI ADR-112).
SELECT
    dbo.CONTACTS.CONTACTID,
    dbo.CONTACTS.SURNAME,
    dbo.CONTACTS.FIRSTNAME,
    dbo.CATEGORIES.CATEGORYNAME,
    dbo.CATEGORIES.CATEGORYCODE
FROM dbo.CONTACTS
INNER JOIN dbo.CATEGORIES
    ON dbo.CONTACTS.CATEGORYCODE = dbo.CATEGORIES.CATEGORYCODE
WHERE dbo.CONTACTS.SURNAME IS NOT NULL
  AND LEN(RTRIM(dbo.CONTACTS.SURNAME)) > 0
