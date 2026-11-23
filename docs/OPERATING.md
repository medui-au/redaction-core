# Running the service

```
pip install 'bps-redaction[service,bp]'      # or: pip install -e '.[service,bp]'

# Make these once and keep them (an env file only the service user can read).
# Losing the encryption key loses only open sessions (24 h) and flagged content.
export BPS_REDACT_KEYS=$(bps-redact keygen)            # keep it out of the database's backups
export BPS_REDACT_AUDIT_SECRET=$(bps-redact keygen audit)
export BPS_REDACT_DB=/var/lib/bps-redact/service.sqlite
export BPS_REDACT_SOURCE=bp
export BPS_REDACT_BP_DSN='DRIVER={ODBC Driver 18 for SQL Server};SERVER=…;DATABASE=BPSPatients;UID=…;PWD=…;TrustServerCertificate=yes'

bps-redact admin add setup                            # a local admin (asks for a password)
bps-redact serve                                      # 127.0.0.1:8470
```

Then open `http://127.0.0.1:8470/admin`, log in, and make API keys under
**API keys** (or `bps-redact keys add my-app --scopes redact,deredact`, which
prints the key once).

To try it without BP: `BPS_REDACT_SOURCE=fixture` and
`BPS_REDACT_FIXTURE=tests/fixtures/practice.json` (invented people).

All settings are environment variables; see `src/bps_redaction_service/config.py`.

## Security

- It binds to `127.0.0.1`. To serve a LAN, set `BPS_REDACT_HOST` and put a
  TLS reverse proxy in front; don't expose it unencrypted.
- The BP login should be a read-only one. The service only runs its own
  SELECT queries (`adapters/sql/`) and refuses any statement that starts as a
  write; the read-only login is what actually guarantees it.
- **Encryption key:** token originals and session keys are encrypted with
  `BPS_REDACT_KEYS`, which is never stored in the database, so a copied file
  alone reveals no patient's identifiers. It is not empty of sensitive data,
  though: the `cache` table holds the practice address book (practitioner
  names) and the nickname table, and the admin lists are plain text. Protect
  the file like the practice's other data. To rotate, put the new key first and keep the old one after it
  (`new,old`) until a week has passed; then drop the old one.
- **Audit secret:** flags and the audit hold a keyed hash of the patient id,
  never the id. `BPS_REDACT_AUDIT_SECRET` is that key; keep it as long as you
  want to tell one patient's flags apart from another's over time, and out of
  the database (patient ids are small numbers: anyone with the secret and the
  file could match hashes to ids).
- **Backups:** the `sessions` and `session_tokens` tables are worthless after
  24 hours and are the only health information held; leave them out of
  backups if you can. The rest (keys, lists, cache, audit) is worth keeping.
- Session ids are bearer-like: the service has no access log for that reason.
  Don't log them in a proxy either.

## The /admin pages

Flags (what the checker found in the service's own output) can be listed,
opened with their hits highlighted while the content is held, and reviewed.
API keys can be made and revoked. Every administrator has the same rights.

Who can log in:
- **BP users in an admin group** (`BPS_REDACT_ADMIN_GROUPS`, default `1,3`:
  Practice Manager and Practice Owner), with their BP
  password. The service checks it against BP's USERS table through the same
  read-only login, and stores nothing about them.
- **Local admins**, made with `bps-redact admin add NAME` (`admin list`,
  `admin remove NAME`). For whoever installs the service, and for when BP can't
  be reached. Passwords are at least 12 characters, stored as scrypt hashes.

A login lasts 30 minutes unused and 8 hours at most
(`BPS_REDACT_ADMIN_IDLE_MINUTES`, `BPS_REDACT_ADMIN_CAP_HOURS`). Five failed
attempts lock that name for 15 minutes. Behind an HTTPS proxy, set
`BPS_REDACT_SECURE_COOKIE=1`. BP's own password hash is one round of SHA-256,
so keep `/admin` on the practice network: the lockout slows guessing, it
doesn't make a weak password strong. Logins, reviews and key changes are
written to the audit table with who made them.

## Lists

The always-redact and name-variant lists are imported from CSV or JSON:

```
bps-redact lists import always_redact always_redact.csv   # columns: string, label, active, fixed_text
bps-redact lists import name_variants name_variants.csv   # columns: name, form, active
bps-redact lists import first_names first_names.csv       # columns: name, include (true adds, false removes)
bps-redact lists export always_redact
```

Import replaces the list. Changes apply within a minute.
