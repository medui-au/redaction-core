-- Active BP users in the groups that may use the service's /admin page
-- (BPS_REDACT_ADMIN_GROUPS, default Practice Manager 1 and Practice Owner 3),
-- for the login list. Takes the group codes as a list.
SELECT
    LTRIM(RTRIM(Firstname)) + ' ' + LTRIM(RTRIM(Surname)) AS Name,
    USERID AS UserId
FROM USERS
WHERE GroupCode IN ({placeholders}) AND recordstatus = 1 AND userstatus IN (1, 2)
ORDER BY Surname
