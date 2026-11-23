-- One active BP user, with the password hash and salt to check a login against
-- (SHA-256 of password + salt, upper-case hex) and the group code.
SELECT USERID, Firstname, Surname, PasswordSalt, Password, GroupCode
FROM USERS
WHERE USERID = ? AND recordstatus = 1 AND userstatus IN (1, 2)
