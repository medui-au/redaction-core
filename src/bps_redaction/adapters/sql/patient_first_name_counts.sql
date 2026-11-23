-- How many patient records hold each first name (all records: carers and
-- relatives are often past patients). The first-name sweep (ADR-058) takes the
-- first word of each and keeps those held by enough patients.
SELECT LTRIM(RTRIM(FirstName)) AS first_name, COUNT(*) AS n
FROM PATIENTS
WHERE FirstName IS NOT NULL
  AND LEN(LTRIM(RTRIM(FirstName))) > 2
GROUP BY LTRIM(RTRIM(FirstName))
