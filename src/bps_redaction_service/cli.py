"""bps-redact: run the service and manage its keys and lists.

  bps-redact keygen                          a new encryption key for BPS_REDACT_KEYS
  bps-redact keygen audit                    a new secret for BPS_REDACT_AUDIT_SECRET
  bps-redact serve                           run the HTTP service
  bps-redact keys add NAME --scopes redact,deredact
  bps-redact keys list | keys revoke NAME
  bps-redact admin add NAME                  a local admin for /admin (asks for a password)
  bps-redact admin list | admin remove NAME
  bps-redact lists import always_redact FILE.json|FILE.csv   (replaces the list)
  bps-redact lists export always_redact
  bps-redact purge                           delete expired sessions now
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .store import LISTS, SCOPES, Store


def _store(settings: Settings) -> Store:
    if not settings.fernet_keys:
        sys.exit('BPS_REDACT_KEYS is not set (make one with: bps-redact keygen)')
    return Store(settings.db_path, settings.fernet_keys)


def _when(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%d %H:%M') if ts else '-'


def _read_rows(path: Path) -> list[dict]:
    if path.suffix.lower() == '.csv':
        with path.open(newline='') as fh:
            return list(csv.DictReader(fh))
    rows = json.loads(path.read_text())
    if not isinstance(rows, list):
        sys.exit('expected a JSON list of rows')
    return rows


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog='bps-redact', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('keygen').add_argument('kind', nargs='?', choices=('encryption', 'audit'),
                                          default='encryption')
    sub.add_parser('serve')
    sub.add_parser('purge')
    keys = sub.add_parser('keys').add_subparsers(dest='action', required=True)
    add = keys.add_parser('add')
    add.add_argument('name')
    add.add_argument('--scopes', default='redact,deredact',
                     help=f'comma-separated, from: {",".join(sorted(SCOPES))}')
    keys.add_parser('list')
    keys.add_parser('revoke').add_argument('name')
    admin = sub.add_parser('admin').add_subparsers(dest='action', required=True)
    admin.add_parser('add').add_argument('name')
    admin.add_parser('list')
    admin.add_parser('remove').add_argument('name')
    lists = sub.add_parser('lists').add_subparsers(dest='action', required=True)
    imp = lists.add_parser('import')
    imp.add_argument('list', choices=LISTS)
    imp.add_argument('file', type=Path)
    lists.add_parser('export').add_argument('list', choices=LISTS)
    args = ap.parse_args(argv)
    settings = Settings.from_env()

    if args.cmd == 'keygen':
        if args.kind == 'audit':
            import secrets
            print(secrets.token_urlsafe(32))
        else:
            from cryptography.fernet import Fernet
            print(Fernet.generate_key().decode())
    elif args.cmd == 'serve':
        import uvicorn
        from .app import create_app
        # No access log: paths carry session ids, which are bearer-like.
        uvicorn.run(create_app(settings), host=settings.host, port=settings.port, access_log=False)
    elif args.cmd == 'purge':
        print(f'{_store(settings).purge_expired()} expired session(s) purged')
    elif args.cmd == 'keys':
        store = _store(settings)
        if args.action == 'add':
            key = store.add_key(args.name, {s.strip() for s in args.scopes.split(',') if s.strip()})
            print(key)
            print('Shown once: store it now. Only its hash is kept.', file=sys.stderr)
        elif args.action == 'list':
            for k in store.list_keys():
                state = f'revoked {_when(k["revoked_at"])}' if k['revoked_at'] else 'active'
                print(f'{k["name"]:20} {k["scopes"]:28} created {_when(k["created_at"])}  '
                      f'last used {_when(k["last_used_at"])}  {state}')
        else:
            print('revoked' if store.revoke_key(args.name) else 'no active key by that name')
    elif args.cmd == 'admin':
        store = _store(settings)
        if args.action == 'add':
            import getpass
            password = getpass.getpass('Password (12 characters or more): ')
            if password != getpass.getpass('Again: '):
                sys.exit('the passwords differ')
            try:
                store.add_admin(args.name, password)
            except ValueError as exc:
                sys.exit(str(exc))
            except sqlite3.IntegrityError:
                sys.exit(f'there is already a local admin called {args.name}')
            store.audit(key_id=None, action='admin_user_add', outcome=args.name, actor='cli')
            print(f'local admin {args.name} added: log in at /admin')
        elif args.action == 'list':
            for a in store.list_admins():
                print(f'{a["name"]:20} added {_when(a["created_at"])}')
        else:
            removed = store.remove_admin(args.name)
            if removed:
                store.audit(key_id=None, action='admin_user_remove', outcome=args.name, actor='cli')
            print('removed' if removed else 'no local admin by that name')
    elif args.cmd == 'lists':
        store = _store(settings)
        if args.action == 'import':
            print(f'{store.replace_list(args.list, _read_rows(args.file))} row(s) in {args.list}')
        else:
            print(json.dumps(store.list_rows(args.list), indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
