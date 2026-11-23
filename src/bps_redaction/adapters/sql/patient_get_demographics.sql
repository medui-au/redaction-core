WITH RankedData AS (
    SELECT
        InternalId,
        DataCode,
        DataName,
        DataValue,
        ISNULL(CONVERT(VARCHAR(10), ObsDate, 23), '') AS DateName,
        ROW_NUMBER() OVER (PARTITION BY InternalId, DataCode ORDER BY ObsDate DESC) AS RowNum
    FROM Observations
    WHERE DataValue IS NOT NULL AND RECORDSTATUS < 2
),
NextAppointment AS (
    SELECT
        A.InternalId,
        ISNULL(
            FORMAT(DATEADD(second, A.APPOINTMENTTIME, A.APPOINTMENTDATE), 'yyyy-MM-dd HH:mm') + ' ' +
            RTRIM(LEFT(U.FIRSTNAME, 1) + LEFT(U.SURNAME, 1)) + ' ' +
            CAST(DATEDIFF(dd, A.Created, GETDATE()) AS VARCHAR) + 'd ago' +
            ' ' + TRIM(A.REASON),
        '') AS NEXT_APPOINTMENT_VALUE,
        ROW_NUMBER() OVER (PARTITION BY A.InternalId ORDER BY A.AppointmentDate, A.AppointmentTime) AS rn
    FROM APPOINTMENTS AS A
    INNER JOIN USERS AS U ON A.USERID = U.USERID
    WHERE A.RECORDStatus = 1 AND DATEDIFF(dd, GETDATE(), A.AppointmentDate) > -1
),
NokPerson AS (
    -- Collapse rows representing the same person into one, keeping whichever
    -- was entered most recently (freshest phone/relationship details). The
    -- same contact can legitimately appear as more than one NEXTOFKIN row:
    -- Best Practice's "also use as Emergency Contact" tickbox creates a
    -- second TYPE=2 row for a person who already has a TYPE=1 (Next of Kin)
    -- entry, and separately, plain data-entry corrections sometimes insert a
    -- new row rather than updating the old one in place (both left active).
    -- Matched by name since that's the only thing guaranteed present on
    -- every row (phone/relationship can differ between duplicate entries
    -- for the same person, or be blank on one of them).
    SELECT
        NK.InternalId, NK.FIRSTNAME, NK.SURNAME, NK.CONTACTPHONE, NK.CONTACTPHONE2,
        NK.RELATIONSHIP, NK.TYPE, NK.NextofKinid,
        ROW_NUMBER() OVER (
            PARTITION BY NK.InternalId, LOWER(TRIM(NK.FIRSTNAME) + ' ' + TRIM(NK.SURNAME))
            ORDER BY NK.NextofKinid DESC
        ) AS rn_person
    FROM NEXTOFKIN AS NK
    WHERE NK.recordstatus = 1
      AND LEN(TRIM(ISNULL(NK.FIRSTNAME,'')) + TRIM(ISNULL(NK.SURNAME,''))) > 0
),
NokRanked AS (
    -- TYPE 1 = Next of Kin, TYPE 2 = Emergency Contact — Next of Kin ranked
    -- ahead of Emergency Contact, most-recently-entered first within each,
    -- so the two contacts shown are Next of Kin first (if any), then the
    -- Emergency Contact, rather than whichever record happens to have the
    -- highest id (previously always the Emergency Contact, since BP creates
    -- that row with the later id in the paired-entry case).
    SELECT
        InternalId,
        TRIM(FIRSTNAME) + ' ' +
        TRIM(SURNAME) + ' ' +
        REPLACE(CONTACTPHONE, ' ', '') + ' ' +
        REPLACE(CONTACTPHONE2, ' ', '') + ' ' +
        TRIM(RELATIONSHIP) AS NOK_VALUE,
        TRIM(FIRSTNAME) AS NOK_FIRSTNAME,
        TRIM(SURNAME)   AS NOK_SURNAME,
        REPLACE(TRIM(CONTACTPHONE), ' ', '') AS NOK_PHONE1,
        REPLACE(TRIM(CONTACTPHONE2), ' ', '') AS NOK_PHONE2,
        ROW_NUMBER() OVER (PARTITION BY InternalId ORDER BY TYPE ASC, NextofKinid DESC) AS rn_rank
    FROM NokPerson
    WHERE rn_person = 1
),
TopNOK AS (
    -- Raw phone numbers carried through separately (not just baked into the
    -- NOK1/NOK2 display strings) so the repository layer can attempt a
    -- "does this match another patient's mobile" lookup without re-parsing
    -- the display text.
    SELECT
        InternalId,
        MAX(CASE WHEN rn_rank = 1 THEN NOK_VALUE END) AS NOK1,
        MAX(CASE WHEN rn_rank = 1 THEN NOK_FIRSTNAME END) AS NOK1_FIRSTNAME,
        MAX(CASE WHEN rn_rank = 1 THEN NOK_SURNAME END)   AS NOK1_SURNAME,
        MAX(CASE WHEN rn_rank = 1 THEN NOK_PHONE1 END) AS NOK1_PHONE1,
        MAX(CASE WHEN rn_rank = 1 THEN NOK_PHONE2 END) AS NOK1_PHONE2,
        MAX(CASE WHEN rn_rank = 2 THEN NOK_VALUE END) AS NOK2,
        MAX(CASE WHEN rn_rank = 2 THEN NOK_FIRSTNAME END) AS NOK2_FIRSTNAME,
        MAX(CASE WHEN rn_rank = 2 THEN NOK_SURNAME END)   AS NOK2_SURNAME,
        MAX(CASE WHEN rn_rank = 2 THEN NOK_PHONE1 END) AS NOK2_PHONE1,
        MAX(CASE WHEN rn_rank = 2 THEN NOK_PHONE2 END) AS NOK2_PHONE2
    FROM NokRanked
    WHERE rn_rank <= 2
    GROUP BY InternalId
),
LatesteGFR AS (
    SELECT
        CombinedResults.InternalId,
        CASE
            WHEN CombinedResults.RESULTVALUE LIKE '%[^0-9.]%' AND CombinedResults.RESULTVALUE NOT LIKE '<%'
            THEN TRY_CAST(
                     REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                         CombinedResults.RESULTVALUE, '>', ''),
                         '^', ''), '.', ''), '~', ''), '#', ''), '$', ''), '%', ''), '&', '')
                     AS INT)
            WHEN CombinedResults.RESULTVALUE LIKE '<%' AND TRY_CAST(SUBSTRING(CombinedResults.RESULTVALUE, 2, LEN(CombinedResults.RESULTVALUE)) AS INT) IS NOT NULL
            THEN CAST(TRIM(SUBSTRING(CombinedResults.RESULTVALUE, 2, LEN(CombinedResults.RESULTVALUE))) AS INT)
            ELSE TRY_CAST(TRIM(CombinedResults.RESULTVALUE) AS INT)
        END AS LAST_eGFR,
        ISNULL(CONVERT(VARCHAR(10), CombinedResults.REPORTDATE, 23), '') AS LAST_eGFR_DATE,
        ROW_NUMBER() OVER (PARTITION BY CombinedResults.InternalId ORDER BY CombinedResults.REPORTDATE DESC) AS rn_egfr
    FROM (
        SELECT
            IR.INTERNALID,
            IV.RESULTVALUE,
            IV.REPORTDATE
        FROM INCOMINGREPORTS IR
        INNER JOIN INCOMINGVALUES IV ON IR.REPORTID = IV.REPORTID
        WHERE (IR.INTERNALID = ?)
        AND (IV.LOINCCODE IN ('33914-3', '15423-7', '62238-1', 'AUSLAB^EGFR'))
        UNION ALL
        SELECT
            internalid,
            Resultvalue,
            reportdate
        FROM REPORTVALUES
        WHERE internalid = ?
        AND (LOINCCODE IN ('33914-3', '15423-7', '62238-1', 'AUSLAB^EGFR'))
    ) AS CombinedResults
)
SELECT
    P.InternalId AS INTERNALID,
    TRIM(P.FirstName) AS FIRSTNAME,
    TRIM(P.MiddleName) AS MIDDLENAME,
    TRIM(P.Surname) AS SURNAME,
    ISNULL(CONVERT(VARCHAR(10), P.DOB, 23), '') AS DOB,
    TRIM(PS.StatusText) AS STATUSTEXT,
    CASE WHEN SexCode = 1 THEN 'Female' WHEN SexCode = 2 then 'Male' ELSE 'Unknown' END AS GENDER,
    (TRIM(P.MEDICARENO) + ' ' + P.MEDICARELINENO + ' ' + TRIM(ISNULL(P.MEDICAREEXPIRY, ''))) AS MEDICARENO,
    TRIM(P.PensionNo) AS PENSIONNO,
    TRIM(P.DVANO) AS DVANO,
    P.IHI AS IHI,
    TRIM(P.ADDRESS1) + CASE WHEN LEN(TRIM(P.ADDRESS2)) > 0 THEN ' ' + TRIM(P.ADDRESS2) ELSE '' END +
    ', ' + TRIM(P.CITY) + ' ' + TRIM(P.POSTCODE) AS ADDRESS,
    '(H) ' + REPLACE(TRIM(P.HOMEPHONE), ' ', '') AS PHONE_HOME,
    '(M) ' + REPLACE(TRIM(P.MOBILEPHONE), ' ', '') AS PHONE_MOBILE,
    '(W) ' + REPLACE(TRIM(P.WORKPHONE), ' ', '') AS PHONE_WORK,
    CASE WHEN P.PreferredName IS NULL THEN TRIM(P.FirstName) + ' ' + TRIM(P.Surname) ELSE TRIM(P.FirstName) + ' "' + TRIM(P.PreferredName) + '" ' + TRIM(P.Surname) END AS NAME,
    DATEDIFF(YEAR, P.DOB, GETDATE()) - CASE WHEN MONTH(P.DOB) > MONTH(GETDATE()) OR (MONTH(P.DOB) = MONTH(GETDATE()) AND DAY(P.DOB) > DAY(GETDATE())) THEN 1 ELSE 0 END AS AGE,
    PE.EMail AS EMAIL,
    MAX(CASE WHEN RD.DataCode = '7' AND RD.DataValue IS NOT NULL THEN CAST(TRY_CAST(TRIM(RD.DataValue) AS DECIMAL(6,2)) / 100.00 AS DECIMAL(5,2)) ELSE NULL END) AS HEIGHTVALUE,  -- TRY_CAST: a value typed with its unit is blank, not a failed identifier load (0.2.15)
    ISNULL(MAX(CASE WHEN RD.DataCode = '7' THEN RD.DateName END), '') AS HEIGHT_DATE,
    MAX(CASE WHEN RD.DataCode = '8' AND RD.DataValue IS NOT NULL THEN TRY_CAST(TRIM(RD.DataValue) AS DECIMAL) ELSE NULL END) AS WEIGHTVALUE,  -- TRY_CAST, as above (0.2.15)
    ISNULL(MAX(CASE WHEN RD.DataCode = '8' THEN RD.DateName END), '') AS WEIGHT_DATE,
    ISNULL(MAX(CASE WHEN RD.DataCode = '3' THEN TRIM(RD.DataValue) END), '') AS SYSTOLICBP,
    ISNULL(MAX(CASE WHEN RD.DataCode = '3' THEN RD.DateName END), '') AS SYSTOLICBP_DATE,
    ISNULL(MAX(CASE WHEN RD.DataCode = '4' THEN TRIM(RD.DataValue) END), '') AS DIASTOLICBP,
    ISNULL(MAX(CASE WHEN RD.DataCode = '4' THEN RD.DateName END), '') AS DIASTOLICBP_DATE,
    ISNULL(NA.NEXT_APPOINTMENT_VALUE, '') AS NEXT_APPOINTMENT,
    TNK.NOK1 AS NOK,
    -- Name parts and the bare Medicare card number are carried separately for
    -- RedactionService: the composite strings above are display text and never
    -- appear verbatim in a consultation, so they redact nothing.
    TNK.NOK1_FIRSTNAME AS NOK1_FIRSTNAME,
    TNK.NOK1_SURNAME AS NOK1_SURNAME,
    TNK.NOK2_FIRSTNAME AS NOK2_FIRSTNAME,
    TNK.NOK2_SURNAME AS NOK2_SURNAME,
    TRIM(ISNULL(P.PreferredName, '')) AS PREFERREDNAME,
    TRIM(ISNULL(P.ADDRESS1, '')) AS ADDRESS1_PART,
    TRIM(ISNULL(P.ADDRESS2, '')) AS ADDRESS2_PART,
    TRIM(ISNULL(P.CITY, '')) AS CITY_PART,
    TRIM(ISNULL(P.POSTCODE, '')) AS POSTCODE_PART,
    TRIM(ISNULL(P.MEDICARENO, '')) AS MEDICARENO_CARD,
    TNK.NOK1_PHONE1 AS NOK1_PHONE1,
    TNK.NOK1_PHONE2 AS NOK1_PHONE2,
    TNK.NOK2 AS NOK2,
    TNK.NOK2_PHONE1 AS NOK2_PHONE1,
    TNK.NOK2_PHONE2 AS NOK2_PHONE2,
    ISNULL(CAST(LE.LAST_eGFR AS VARCHAR(10)), '') AS LAST_eGFR,
    ISNULL(LE.LAST_eGFR_DATE, '') AS LAST_eGFR_DATE,
    P.MyMedicare,
    P.MyMedicareRegisteredLocation,
    P.MyMedicareRegistrationDate
FROM Patients AS P
LEFT JOIN Email AS PE ON P.InternalId = PE.InternalId
LEFT JOIN RankedData AS RD ON P.InternalId = RD.InternalId AND RD.RowNum = 1
LEFT JOIN PatientStatus AS PS ON P.PatientStatus = PS.StatusCode
LEFT JOIN NextAppointment AS NA ON P.InternalId = NA.InternalId AND NA.rn = 1
LEFT JOIN TopNOK AS TNK ON P.InternalId = TNK.InternalId
LEFT JOIN LatesteGFR AS LE ON P.InternalId = LE.InternalId AND LE.rn_egfr = 1
WHERE P.InternalId = ?
GROUP BY
    P.InternalId,
    P.FirstName,
    P.MiddleName,
    P.Surname,
    P.DOB,
    PS.StatusText,
    SEXCODE,
    P.MEDICARENO,
    P.MEDICARELINENO,
    P.MEDICAREEXPIRY,
    P.PensionNo,
    P.ADDRESS1,
    P.ADDRESS2,
    P.CITY,
    P.POSTCODE,
    P.HOMEPHONE,
    P.MOBILEPHONE,
    P.WORKPHONE,
    P.IHI,
    P.PREFERREDNAME,
    P.DVANO,
    PE.EMAIL,
    P.MyMedicare,
    P.MyMedicareRegisteredLocation,
    P.MyMedicareRegistrationDate,
    NA.NEXT_APPOINTMENT_VALUE,
    TNK.NOK1,
    TNK.NOK1_FIRSTNAME,
    TNK.NOK1_SURNAME,
    TNK.NOK2_FIRSTNAME,
    TNK.NOK2_SURNAME,
    TNK.NOK1_PHONE1,
    TNK.NOK1_PHONE2,
    TNK.NOK2,
    TNK.NOK2_PHONE1,
    TNK.NOK2_PHONE2,
    LE.LAST_eGFR,
    LE.LAST_eGFR_DATE;