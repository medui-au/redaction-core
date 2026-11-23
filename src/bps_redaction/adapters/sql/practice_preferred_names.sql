-- Patients with a preferred name recorded, for the practice nickname table
-- (ADR-048). Only aggregates leave the service: a first name -> preferred
-- form pairing is kept only when 3 or more patients share it.
SELECT FirstName, MiddleName, Surname, PreferredName
FROM PATIENTS
WHERE RecordStatus = 1
  AND PreferredName IS NOT NULL
  AND LEN(LTRIM(RTRIM(PreferredName))) > 0;
