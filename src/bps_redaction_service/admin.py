"""The /admin pages: review flags, and issue and revoke API keys.

Two kinds of administrator, both with the same rights:
  BP users      in an admin group (BPS_REDACT_ADMIN_GROUPS, default 1 Practice
                Manager and 3 Practice Owner), with their BP password; nothing
                about them is stored here
  local admins  made with `bps-redact admin add`, for whoever installs the
                service, and for when BP can't be reached

Server-rendered, no scripts. The login cookie holds a random id (only its hash
is stored), is HttpOnly and SameSite=Strict, and every form carries the
session's CSRF token. Failed logins lock the name out for a while.
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import html
import sqlite3
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .store import DISPOSITIONS, SCOPES, Store

COOKIE = 'bpsr_admin'
_LOCK_AFTER = 5            # failed logins …
_LOCK_WINDOW = 15 * 60     # … within this many seconds lock the name for as long

_SCOPE_NOTES = {
    'redact': 'redact text and records (POST /v1/redact)',
    'deredact': 'restore replies (POST /v1/deredact)',
    'verify': 'check text that did not come through /redact',
    'flags': 'read and review all flags over the API',
    'admin': 'reserved',
}


class _Lockout:
    def __init__(self):
        self._fails: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def _recent(self, who: str, now: float) -> collections.deque:
        fails = self._fails[who]
        while fails and now - fails[0] > _LOCK_WINDOW:
            fails.popleft()
        return fails

    def locked(self, who: str) -> bool:
        with self._lock:
            return len(self._recent(who, time.monotonic())) >= _LOCK_AFTER

    def fail(self, who: str) -> None:
        with self._lock:
            self._recent(who, time.monotonic()).append(time.monotonic())

    def clear(self, who: str) -> None:
        with self._lock:
            self._fails.pop(who, None)


def bp_password_ok(record: dict, password: str) -> bool:
    """BP's check: SHA-256 of the password then the salt, upper-case hex."""
    stored = (record.get('Password') or '').strip().upper()
    salt = str(record.get('PasswordSalt') or '')
    if not stored or not salt:
        return False
    computed = hashlib.sha256(password.encode() + salt.encode()).hexdigest().upper()
    return hmac.compare_digest(computed, stored)


# ── page furniture ────────────────────────────────────────────────────────────

_CSS = """
:root{--bg:#fbfaf8;--fg:#1d1d1b;--muted:#6b6a66;--line:#e2dfd8;--panel:#fff;--accent:#2b5f8a;
--hit:#ffe08a;--bad:#a33a2b;--ok:#2f6b3a}
@media (prefers-color-scheme:dark){:root{--bg:#191917;--fg:#ecebe7;--muted:#a3a19b;--line:#3a3935;
--panel:#22221f;--accent:#8ab8de;--hit:#7a5d00;--bad:#e58b7d;--ok:#8fcf99}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;gap:16px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);
flex-wrap:wrap}header b{margin-right:auto}header a{color:var(--accent);text-decoration:none}
main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:20px;margin:8px 0 12px}
h2{font-size:16px;margin:20px 0 8px}table{border-collapse:collapse;width:100%;background:var(--panel)}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:13px}.wrap{overflow-x:auto}
.muted{color:var(--muted)}.bad{color:var(--bad)}.ok{color:var(--ok)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:12px;margin:12px 0}
.tiles{display:flex;gap:12px;flex-wrap:wrap}.tile{min-width:120px}.tile b{display:block;font-size:22px}
pre{white-space:pre-wrap;word-break:break-word;background:var(--panel);border:1px solid var(--line);
padding:12px;border-radius:6px;font-size:13px}mark{background:var(--hit);color:inherit}
input,select,textarea,button{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:4px;
background:var(--panel);color:var(--fg)}button{cursor:pointer}button.primary{background:var(--accent);
color:var(--bg);border-color:var(--accent)}form.inline{display:inline}label{display:block;margin:6px 0}
.key{font-family:ui-monospace,monospace;font-size:14px;padding:10px;border:2px dashed var(--accent);
word-break:break-all}
.titlebar{display:flex;align-items:center;gap:10px}.titlebar h1{margin-right:auto}
[popover]{max-width:min(680px,calc(100vw - 32px));max-height:calc(100vh - 64px);overflow:auto;
background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:16px 20px}
[popover]::backdrop{background:rgb(0 0 0 / .35)}[popover] h2:first-child{margin-top:0}
[popover] td,[popover] th{font-size:14px}
"""


_KEYS_HELP = """
<div id="keys-help" popover>
<h2>About API keys</h2>
<p>A key is what a client program shows on every request
(<code>Authorization: Bearer bpsr_…</code>) to use <code>/v1/redact</code>,
<code>/v1/deredact</code> and the rest of the API. A new service has none: make one
when something is going to call it — a script you’re testing with, or an
application connected to it. These admin pages don’t need a key; you log in here
with your BP account or as a local admin.</p>
<h2>Making a key</h2>
<p><b>Name</b> — who or what will use it (<code>clinic-app</code>, <code>script-testing</code>).
Names are unique, and they appear in the flag list, so you can tell which client
a flag came from.</p>
<p><b>Scopes</b> — what the key may do:</p>
<table>
<tr><th>Scope</th><th>Allows</th></tr>
<tr><td><code>redact</code></td><td>send text or records and get them back redacted (<code>POST /v1/redact</code>)</td></tr>
<tr><td><code>deredact</code></td><td>put the real details back into a model’s reply (<code>POST /v1/deredact</code>)</td></tr>
<tr><td><code>verify</code></td><td>check text that didn’t come through <code>/redact</code> for leaks</td></tr>
<tr><td><code>flags</code></td><td>read and review <b>all</b> flags over the API, e.g. for a dashboard; leave it off ordinary client keys</td></tr>
</table>
<p>The key is shown <b>once</b>. Copy it into the client’s settings straight away:
only a fingerprint of it is kept, so it can’t be shown again. If it’s lost,
revoke it and make another.</p>
<h2>Good habits</h2>
<ul>
<li><b>One key per client program</b>, so you can revoke one without breaking the
others, and flags and the audit show which client did what.</li>
<li><b>Revoking ends that key’s sessions at once.</b> Replies still waiting to be
de-redacted under it can no longer be restored. Revoke when a key may have
leaked or a client is retired.</li>
<li>An ordinary client needs only <code>redact</code> and <code>deredact</code>.</li>
</ul>
<p><button popovertarget="keys-help" popovertargetaction="hide">Close</button></p>
</div>
"""


def _e(value) -> str:
    return html.escape('' if value is None else str(value))


def _when(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if ts else '–'


def _page(title: str, body: str, who: dict | None = None) -> HTMLResponse:
    nav = ''
    if who:
        nav = (f'<a href="/admin/flags">Flags</a><a href="/admin/keys">API keys</a>'
               f'<span class="muted">{_e(who["who"])}</span>'
               f'<form class="inline" method="post" action="/admin/logout">'
               f'<input type="hidden" name="csrf" value="{_e(who["csrf"])}">'
               f'<button>Log out</button></form>')
    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<title>{_e(title)} · BPS Redaction</title><style>{_CSS}</style></head><body>'
           f'<header><b>BPS Redaction · admin</b>{nav}</header><main>{body}</main></body></html>')
    return HTMLResponse(doc, headers={
        'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
        'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; "
                                   "form-action 'self'; frame-ancestors 'none'"})


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _highlight(text: str, hits: list[dict]) -> str:
    out, pos = [], 0
    for h in sorted(hits, key=lambda h: h['start']):
        if h['start'] < pos or h['end'] > len(text):
            continue
        out.append(_e(text[pos:h['start']]))
        out.append(f'<mark title="{_e(h["type"])} (tier {h["tier"]})">{_e(text[h["start"]:h["end"]])}</mark>')
        pos = h['end']
    out.append(_e(text[pos:]))
    return ''.join(out)


# ── the router ────────────────────────────────────────────────────────────────

def admin_router(settings, store: Store, redactor) -> APIRouter:
    router = APIRouter(prefix='/admin', include_in_schema=False)
    lockout = _Lockout()

    def current(request: Request) -> dict | None:
        cookie = request.cookies.get(COOKIE)
        return store.admin_session(cookie, settings.admin_idle, settings.admin_cap) if cookie else None

    def csrf_ok(who: dict, token: str) -> bool:
        return hmac.compare_digest(who['csrf'], token or '')

    def bp_admins() -> list[dict] | None:
        """The BP users who may log in, or None when BP can't be asked."""
        if redactor.source is None or not settings.admin_groups:
            return None
        try:
            rows = redactor.source.query('auth_admin_users', settings.admin_groups)
        except Exception:
            return None
        return [{'name': (r.get('Name') or '').strip(), 'id': r.get('UserId')} for r in rows]

    def bp_login(userid: str, password: str) -> str | None:
        """The BP user's display name if this is an admin and the password is right."""
        if redactor.source is None or not userid.isdigit():
            return None
        try:
            rows = redactor.source.query('auth_get_user_by_id', [int(userid)])
        except Exception:
            return None
        if not rows or rows[0].get('GroupCode') not in settings.admin_groups:
            return None
        if not bp_password_ok(rows[0], password):
            return None
        r = rows[0]
        return ' '.join(p for p in ((r.get('Firstname') or '').strip(),
                                    (r.get('Surname') or '').strip()) if p) or f'BP user {userid}'

    def guarded(request: Request):
        who = current(request)
        return who, (None if who else _redirect('/admin/login'))

    # ── login ─────────────────────────────────────────────────────────────────

    def login_page(message: str = '') -> HTMLResponse:
        admins = bp_admins()
        if admins:
            options = ''.join(f'<option value="{_e(a["id"])}">{_e(a["name"])}</option>' for a in admins)
            bp_form = (f'<form method="post" action="/admin/login"><input type="hidden" name="kind" value="bp">'
                       f'<label>BP user<br><select name="user" required>{options}</select></label>'
                       f'<label>BP password<br><input type="password" name="password" required '
                       f'autocomplete="current-password"></label><button class="primary">Log in</button></form>')
        else:
            bp_form = '<p class="muted">BP can’t be reached, or no BP group may log in: local admins only.</p>'
        local_form = ('<form method="post" action="/admin/login"><input type="hidden" name="kind" value="local">'
                      '<label>Name<br><input name="user" required autocomplete="username"></label>'
                      '<label>Password<br><input type="password" name="password" required '
                      'autocomplete="current-password"></label><button>Log in</button></form>')
        note = f'<p class="bad">{_e(message)}</p>' if message else ''
        body = (f'<h1>Log in</h1>{note}<div class="panel"><h2>Practice administrator (BP)</h2>{bp_form}</div>'
                f'<div class="panel"><h2>Local admin</h2><p class="muted">Made with '
                f'<code>bps-redact admin add</code>, for setup and when BP is down.</p>{local_form}</div>')
        return _page('Log in', body)

    @router.get('/login')
    def login_form(request: Request):
        return _redirect('/admin/flags') if current(request) else login_page()

    @router.post('/login')
    def login(kind: str = Form(...), user: str = Form(...), password: str = Form(...)):
        user = user.strip()
        lock_name = f'{kind}:{user.lower()}'
        if lockout.locked(lock_name):
            store.audit(key_id=None, action='admin_login', outcome='locked', actor=user)
            return login_page('Too many failed attempts: try again in 15 minutes.')
        name = None
        if kind == 'local' and store.check_admin(user, password):
            name = user
        elif kind == 'bp':
            name = bp_login(user, password)
        if not name:
            lockout.fail(lock_name)
            store.audit(key_id=None, action='admin_login', outcome='failed', actor=user)
            return login_page('That name and password don’t match an administrator.')
        lockout.clear(lock_name)
        actor = name if kind == 'local' else f'{name} (BP {user})'
        session_id, _csrf = store.start_admin_session(actor, kind)
        store.audit(key_id=None, action='admin_login', outcome='ok', actor=actor)
        response = _redirect('/admin/flags')
        response.set_cookie(COOKIE, session_id, httponly=True, samesite='strict', path='/admin',
                            secure=settings.secure_cookie, max_age=int(settings.admin_cap))
        return response

    @router.post('/logout')
    def logout(request: Request, csrf: str = Form('')):
        who = current(request)
        if who and csrf_ok(who, csrf):
            store.end_admin_session(request.cookies[COOKIE])
            store.audit(key_id=None, action='admin_logout', outcome='ok', actor=who['who'])
        response = _redirect('/admin/login')
        response.delete_cookie(COOKIE, path='/admin')
        return response

    @router.get('')
    def home():
        return _redirect('/admin/flags')

    # ── flags ─────────────────────────────────────────────────────────────────

    @router.get('/flags')
    def flags(request: Request, disposition: str = '', tier: str = '', days: int = 30):
        who, bounce = guarded(request)
        if bounce:
            return bounce
        days = max(1, min(days, 365))
        since = time.time() - days * 86400
        disposition = disposition if disposition in DISPOSITIONS else ''
        tier_n = int(tier) if tier in ('0', '1', '2') else None
        rows = store.list_flags(since=since, tier=tier_n, disposition=disposition or None, limit=500)
        summary = store.flag_summary(since=since)
        names = {k['id']: k['name'] for k in store.list_keys()}

        tiles = ''.join(f'<div class="tile"><b>{summary["by_top_tier"].get(t, 0)}</b>'
                        f'<span class="muted">top tier {t}</span></div>' for t in ('0', '1', '2'))
        tiles = (f'<div class="tile"><b>{summary["flags"]}</b><span class="muted">flags, {days} days</span>'
                 f'</div>{tiles}<div class="tile"><b>{summary["by_disposition"].get("unreviewed", 0)}</b>'
                 f'<span class="muted">unreviewed</span></div>')
        types = ', '.join(f'{_e(t)} {n}' for t, n in sorted(summary['by_type'].items(), key=lambda kv: -kv[1]))

        def choice(name, value, options):
            return (f'<select name="{name}">' + ''.join(
                f'<option value="{_e(v)}"{" selected" if v == value else ""}>{_e(label)}</option>'
                for v, label in options) + '</select>')
        filters = (f'<form method="get" action="/admin/flags" class="panel">'
                   f'{choice("disposition", disposition, [("", "any review state")] + [(d, d.replace("_", " ")) for d in DISPOSITIONS])} '
                   f'{choice("tier", tier, [("", "any top tier"), ("0", "tier 0: the patient"), ("1", "tier 1: practice"), ("2", "tier 2: shapes")])} '
                   f'{choice("days", str(days), [(str(d), f"last {d} days") for d in (1, 7, 30, 90)])} '
                   f'<button>Show</button></form>')
        body_rows = ''.join(
            f'<tr><td><a href="/admin/flags/{_e(r["id"])}">{_when(r["at"])}</a></td>'
            f'<td>{_e(names.get(r["key_id"], r["key_id"]))}</td><td>{_e(", ".join(r["profiles"]))}</td>'
            f'<td>{r["top_tier"]}</td><td>{_e(", ".join(f"{t} {n}" for t, n in r["counts"].items()))}</td>'
            f'<td>{_e(r["disposition"].replace("_", " "))}</td></tr>' for r in rows)
        table = (f'<div class="wrap"><table><tr><th>When</th><th>Key</th><th>Profiles</th><th>Top tier</th>'
                 f'<th>Hits</th><th>Review</th></tr>{body_rows}</table></div>' if rows
                 else '<p class="muted">No flags match.</p>')
        body = (f'<h1>Flags</h1><div class="panel tiles">{tiles}</div>'
                f'<p class="muted">By type: {types or "none"}</p>{filters}{table}')
        return _page('Flags', body, who)

    @router.get('/flags/{flag_id}')
    def flag(request: Request, flag_id: str):
        who, bounce = guarded(request)
        if bounce:
            return bounce
        f = store.get_flag(flag_id)
        if f is None:
            return _page('Not found', '<h1>No such flag</h1><p><a href="/admin/flags">Back</a></p>', who)
        names = {k['id']: k['name'] for k in store.list_keys()}
        hits = ''.join(f'<tr><td>{h["tier"]}</td><td>{_e(h["type"])}</td><td>{_e(h.get("kind"))}</td>'
                       f'<td>{h["start"]}–{h["end"]}</td></tr>' for h in f['hits'])
        if f['content'] is not None:
            content = f'<pre>{_highlight(f["content"], f["hits"])}</pre>'
        else:
            content = '<p class="muted">The content went with its session; the hits above remain.</p>'
        options = ''.join(f'<option value="{d}"{" selected" if d == f["disposition"] else ""}>'
                          f'{d.replace("_", " ")}</option>' for d in DISPOSITIONS)
        review = (f'<form method="post" action="/admin/flags/{_e(flag_id)}/review" class="panel">'
                  f'<input type="hidden" name="csrf" value="{_e(who["csrf"])}">'
                  f'<label>Review<br><select name="disposition">{options}</select></label>'
                  f'<label>Note<br><textarea name="note" rows="2" cols="60" maxlength="2000">{_e(f["note"])}</textarea></label>'
                  f'<button class="primary">Save</button></form>')
        reviewed = (f'<p class="muted">Reviewed by {_e(f["reviewer"])}, {_when(f["reviewed_at"])}</p>'
                    if f['reviewer'] else '')
        body = (f'<p><a href="/admin/flags">← Flags</a></p><h1>Flag {_e(flag_id)}</h1>'
                f'<p class="muted">{_when(f["at"])} · key {_e(names.get(f["key_id"], f["key_id"]))} · '
                f'{_e(", ".join(f["profiles"]))} · ruleset {_e(f["ruleset"])} · detector {_e(f["detector"])}</p>'
                f'<div class="wrap"><table><tr><th>Tier</th><th>Type</th><th>Kind</th><th>Offsets</th></tr>{hits}'
                f'</table></div>{reviewed}{review}<h2>Content as sent back</h2>{content}')
        return _page(f'Flag {flag_id}', body, who)

    @router.post('/flags/{flag_id}/review')
    def review(request: Request, flag_id: str, disposition: str = Form(...), note: str = Form(''),
               csrf: str = Form('')):
        who, bounce = guarded(request)
        if bounce:
            return bounce
        if not csrf_ok(who, csrf) or disposition not in DISPOSITIONS:
            return Response(status_code=400)
        if store.review_flag(flag_id, disposition, who['who'], note.strip()[:2000] or None):
            store.audit(key_id=None, action='admin_review_flag', outcome=disposition, actor=who['who'])
        return _redirect(f'/admin/flags/{flag_id}')

    # ── API keys ──────────────────────────────────────────────────────────────

    def keys_page(who: dict, new_key: tuple[str, str] | None = None, message: str = '') -> HTMLResponse:
        rows = ''.join(
            f'<tr><td>{_e(k["name"])}</td><td>{_e(k["scopes"].replace(",", ", "))}</td>'
            f'<td>{_when(k["created_at"])}</td><td>{_when(k["last_used_at"])}</td><td>'
            + (f'<span class="muted">revoked {_when(k["revoked_at"])}</span>' if k['revoked_at'] else
               f'<form class="inline" method="post" action="/admin/keys/revoke">'
               f'<input type="hidden" name="csrf" value="{_e(who["csrf"])}">'
               f'<input type="hidden" name="name" value="{_e(k["name"])}">'
               f'<button>Revoke</button></form>')
            + '</td></tr>' for k in store.list_keys())
        shown = ''
        if new_key:
            shown = (f'<div class="panel"><h2>Key “{_e(new_key[0])}” made</h2><p>Copy it now: it is shown '
                     f'once, and only its hash is kept.</p><div class="key">{_e(new_key[1])}</div></div>')
        note = f'<p class="bad">{_e(message)}</p>' if message else ''
        boxes = ''.join(f'<label><input type="checkbox" name="scopes" value="{s}"'
                        f'{" checked" if s in ("redact", "deredact") else ""}> {s} '
                        f'<span class="muted">— {_e(_SCOPE_NOTES.get(s, ""))}</span></label>'
                        for s in sorted(SCOPES - {'admin'}))
        add = (f'<form method="post" action="/admin/keys" class="panel"><h2>New key</h2>'
               f'<input type="hidden" name="csrf" value="{_e(who["csrf"])}">'
               f'<label>Name (who or what will use it)<br><input name="name" required maxlength="80"></label>'
               f'{boxes}<button class="primary">Make key</button></form>')
        log_rows = ''.join(f'<tr><td>{_when(a["at"])}</td><td>{_e(a["action"].removeprefix("admin_"))}</td>'
                           f'<td>{_e(a["outcome"])}</td><td>{_e(a["actor"])}</td></tr>'
                           for a in store.recent_audit('admin_key', 20))
        log = (f'<h2>Recent key changes</h2><div class="wrap"><table><tr><th>When</th><th>What</th>'
               f'<th>Key</th><th>By</th></tr>{log_rows}</table></div>' if log_rows else '')
        body = (f'<div class="titlebar"><h1>API keys</h1><button popovertarget="keys-help">'
                f'About API keys</button></div>{_KEYS_HELP}{note}{shown}<p class="muted">Revoking a key ends its sessions at once: '
                f'replies redacted under it can no longer be restored.</p><div class="wrap"><table>'
                f'<tr><th>Name</th><th>Scopes</th><th>Made</th><th>Last used</th><th></th></tr>{rows}'
                f'</table></div>{add}{log}')
        return _page('API keys', body, who)

    @router.get('/keys')
    def keys(request: Request):
        who, bounce = guarded(request)
        return bounce or keys_page(who)

    @router.post('/keys')
    def add_key(request: Request, name: str = Form(...), scopes: list[str] = Form(default=[]),
                csrf: str = Form('')):
        who, bounce = guarded(request)
        if bounce:
            return bounce
        if not csrf_ok(who, csrf):
            return Response(status_code=400)
        name = name.strip()
        chosen = {s for s in scopes if s in SCOPES - {'admin'}}
        if not name or not chosen:
            return keys_page(who, message='A key needs a name and at least one scope.')
        try:
            key = store.add_key(name, chosen)
        except sqlite3.IntegrityError:
            return keys_page(who, message=f'There is already a key called “{name}”.')
        store.audit(key_id=None, action='admin_key_add', outcome=name, actor=who['who'])
        return keys_page(who, new_key=(name, key))

    @router.post('/keys/revoke')
    def revoke_key(request: Request, name: str = Form(...), csrf: str = Form('')):
        who, bounce = guarded(request)
        if bounce:
            return bounce
        if not csrf_ok(who, csrf):
            return Response(status_code=400)
        if store.revoke_key(name):
            store.audit(key_id=None, action='admin_key_revoke', outcome=name, actor=who['who'])
        return _redirect('/admin/keys')

    return router
