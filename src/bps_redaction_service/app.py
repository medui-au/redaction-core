"""The HTTP service: a thin layer over the bps_redaction core.

POST   /v1/redact               redact with one or more profiles, in a session;
                                every response carries the checker's flag report
POST   /v1/deredact             restore a reply from the session's token map
DELETE /v1/sessions/{id}        purge a session now
POST   /v1/verify               check text that didn't come through /redact
GET    /v1/flags                flagged queries (scope 'flags'), filterable
GET    /v1/flags/summary        counts by tier, type, day, disposition
GET    /v1/flags/{id}           one flag, with its content while its session lives
POST   /v1/flags/{id}/review    set a disposition and note
GET    /v1/health               liveness and identity-source reachability
GET    /v1/info                 version, ruleset, limits
/admin                          web pages: flags, reviews, API keys (admin.py)

Never logs or echoes request text. Fails closed: if a patient's identifiers
can't be loaded, no content comes back (plan D6).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import hmac
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

import bps_redaction
from bps_redaction import IdentitySourceUnavailable, Redactor, TokenVault

from .config import Settings
from .store import DISPOSITIONS, ApiKey, Store, StoreCache, StoreLists

log = logging.getLogger('bps_redaction_service')

_TOKEN_RE = re.compile(r'\[Redacted ([A-Za-z]+): [A-Z]+-[0-9a-f]+\]')
_FIXED_RE = re.compile(r'\[Redacted ([A-Za-z]+)\]|\[(Link) removed\]')


class ApiError(Exception):
    def __init__(self, status: int, error: str):
        self.status, self.error = status, error


# ── request / response bodies ─────────────────────────────────────────────────

Profile = Literal['record', 'input', 'prompt']


class RedactIn(BaseModel):
    profiles: list[Profile] = Field(default_factory=lambda: ['prompt'], min_length=1, max_length=3)
    patient_id: int | None = None
    content: str | dict | list
    session_id: str | None = None


class DeredactIn(BaseModel):
    content: str
    session_id: str


class VerifyIn(BaseModel):
    content: str | dict | list
    patient_id: int | None = None


class ReviewIn(BaseModel):
    disposition: Literal['unreviewed', 'true_leak', 'false_positive', 'acceptable']
    note: str | None = Field(default=None, max_length=2000)
    reviewer: str | None = Field(default=None, max_length=200)


MAX_HITS = 20   # in a /redact response; the flag store keeps them all


# ── helpers ───────────────────────────────────────────────────────────────────

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _as_text(content) -> str:
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


def _check(redactor, text: str, patient_id) -> dict | None:
    """The checker's hits on text, or None if it failed (feedback, never a gate)."""
    from bps_redaction.checker.scan import hits
    try:
        return {'hits': hits(redactor.check(text, patient_id))}
    except Exception as exc:
        log.error('checker failed: %s', type(exc).__name__)
        return None


def _summary(text: str) -> dict[str, int]:
    counts: collections.Counter = collections.Counter()
    for label in _TOKEN_RE.findall(text):
        counts[label.lower()] += 1
    for fixed, link in _FIXED_RE.findall(text):
        counts[(fixed or link).lower()] += 1
    return dict(counts)


class _RateLimiter:
    def __init__(self, per_minute: int):
        self._per_minute = per_minute
        self._hits: dict[int, collections.deque] = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def allow(self, key_id: int) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key_id]
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= self._per_minute:
                return False
            hits.append(now)
            return True


class _SizeLimit:
    """Refuse bodies over the cap, counted as they arrive (no Content-Length trust)."""

    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        declared = dict(scope.get('headers') or []).get(b'content-length')
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            return await _too_large(send)
        seen = 0

        async def counted():
            nonlocal seen
            message = await receive()
            if message['type'] == 'http.request':
                seen += len(message.get('body', b''))
                if seen > self.max_bytes:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, counted, send)
        except _BodyTooLarge:
            await _too_large(send)


class _BodyTooLarge(Exception):
    pass


async def _too_large(send):
    body = b'{"error":"request_too_large"}'
    await send({'type': 'http.response.start', 'status': 413,
                'headers': [(b'content-type', b'application/json'),
                            (b'content-length', str(len(body)).encode())]})
    await send({'type': 'http.response.body', 'body': body})


# ── the app ───────────────────────────────────────────────────────────────────

def create_app(settings: Settings, *, store: Store | None = None,
               redactor: Redactor | None = None) -> FastAPI:
    store = store or Store(settings.db_path, settings.fernet_keys)
    redactor = redactor or Redactor(settings.identity_source(), lists=StoreLists(store),
                                    cache=StoreCache(store))
    limiter = _RateLimiter(settings.rate_per_minute)
    if settings.audit_secret:
        audit_key = hashlib.sha256(b'bps-redact-audit:' + settings.audit_secret.encode()).digest()
    else:
        log.warning('BPS_REDACT_AUDIT_SECRET is not set: patient hashes follow the first '
                    'encryption key and will change when it is rotated')
        audit_key = hashlib.sha256(b'bps-redact-audit:' + settings.fernet_keys[0].encode()).digest()
    health_cache: dict[str, Any] = {}

    def patient_hash(patient_id):
        if patient_id is None:
            return None
        return hmac.new(audit_key, str(patient_id).encode(), hashlib.sha256).hexdigest()[:16]

    async def purge_loop():
        while True:
            try:
                n = await asyncio.to_thread(store.purge_expired, settings.flag_days,
                                            settings.admin_idle, settings.admin_cap)
                if n:
                    log.info('purged %d expired session(s)', n)
            except Exception as exc:              # keep purging on the next round
                log.error('purge failed: %s', type(exc).__name__)
            await asyncio.sleep(settings.purge_seconds)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(purge_loop())   # runs once at start-up, then on schedule
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title='BPS Redaction Service', version=bps_redaction.__version__,
                  lifespan=lifespan)
    app.add_middleware(_SizeLimit, max_bytes=settings.max_bytes)
    app.state.store, app.state.redactor = store, redactor
    from .admin import admin_router
    app.include_router(admin_router(settings, store, redactor))

    @app.exception_handler(ApiError)
    async def _api_error(_req, exc: ApiError):
        return JSONResponse({'error': exc.error}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_req, exc: RequestValidationError):
        # Pydantic's default echoes the input back; send where and what only.
        problems = [{'loc': [str(p) for p in e.get('loc', ())], 'msg': e.get('msg', '')}
                    for e in exc.errors()]
        return JSONResponse({'error': 'invalid_request', 'problems': problems}, status_code=422)

    @app.exception_handler(Exception)
    async def _crash(_req, exc: Exception):
        log.error('unhandled %s', type(exc).__name__)     # type only: messages can carry text
        return JSONResponse({'error': 'internal_error'}, status_code=500)

    def caller(authorization: str | None = Header(default=None)) -> ApiKey:
        if not authorization or not authorization.startswith('Bearer '):
            raise ApiError(401, 'unauthorised')
        key = store.find_key(authorization[7:].strip())
        if key is None:
            raise ApiError(401, 'unauthorised')
        if not limiter.allow(key.id):
            raise ApiError(429, 'rate_limited')
        return key

    def need(key: ApiKey, scope: str) -> None:
        if scope not in key.scopes:
            raise ApiError(403, 'forbidden')

    def own_session(key: ApiKey, session_id: str):
        session = store.get_session(session_id)
        if session is None or session.key_id != key.id:
            raise ApiError(404, 'session_not_found')     # another key's id: as if absent
        if session.expires_at <= time.time():
            store.delete_session(session.id)
            raise ApiError(410, 'session_expired')
        return session

    # ── /v1/redact ────────────────────────────────────────────────────────────

    @app.post('/v1/redact')
    def redact(body: RedactIn, key: ApiKey = Depends(caller)):
        need(key, 'redact')
        started = time.monotonic()
        profiles, pid, content = body.profiles, body.patient_id, body.content
        if 'record' in profiles and pid is None:
            raise ApiError(422, 'record_needs_patient_id')
        if 'record' in profiles and isinstance(content, str):
            raise ApiError(422, 'record_needs_structured_content')
        if 'prompt' in profiles and not isinstance(content, str) and profiles.index('prompt') == 0:
            raise ApiError(422, 'prompt_needs_text')

        session = own_session(key, body.session_id) if body.session_id else \
            store.create_session(key.id, settings.session_ttl, settings.session_cap)
        vault = TokenVault(session.vault_key)

        def fail(status, error, outcome):
            store.audit(key_id=key.id, action='redact', outcome=outcome, profiles=profiles,
                        patient_hash=patient_hash(pid),
                        duration_ms=int((time.monotonic() - started) * 1000))
            raise ApiError(status, error)

        try:
            # Fail closed: a patient whose identifiers can't be loaded gets nothing back.
            if pid is not None and not redactor.identifiers(pid):
                fail(503, 'identity_source_unavailable', 'no_identifiers')
            for profile in profiles:
                if profile == 'record':
                    content, _loaded = redactor.record(content, pid, vault)
                elif profile == 'input':
                    if isinstance(content, str):
                        content = redactor.input({'text': content}, pid, vault)['text']
                    elif isinstance(content, dict):
                        content = redactor.input(content, pid, vault)
                    else:
                        fail(422, 'input_needs_text_or_fields', 'invalid')
                else:  # prompt
                    if not isinstance(content, str):
                        fail(422, 'prompt_needs_text', 'invalid')
                    content = redactor.prompt(content, pid, vault)
        except IdentitySourceUnavailable:
            fail(503, 'identity_source_unavailable', 'source_unavailable')
        except ApiError:
            raise
        except Exception as exc:                 # never a partly-redacted text
            log.error('redaction failed: %s', type(exc).__name__)
            fail(500, 'internal_error', 'error')

        text = _as_text(content)
        reverse = vault.get_reverse_map()
        used = {t: reverse[t] for t in set(m.group(0) for m in _TOKEN_RE.finditer(text)) if t in reverse}
        store.add_tokens(session.id, used)
        expires = store.touch_session(session, settings.session_ttl, settings.session_cap)
        summary = _summary(text)
        flags = flag_report(text, pid, key, session.id, profiles)
        store.audit(key_id=key.id, action='redact', outcome='ok', profiles=profiles,
                    patient_hash=patient_hash(pid), counts=summary,
                    duration_ms=int((time.monotonic() - started) * 1000))
        return {'content': content, 'session_id': session.id, 'expires_at': _iso(expires),
                'summary': summary, 'identifiers_loaded': pid is not None,
                'profiles': profiles, 'ruleset': bps_redaction.RULESET, 'flags': flags}

    def flag_report(text, pid, key, session_id, profiles) -> dict:
        """What the checker found in our own output; stored when anything was."""
        checked = _check(redactor, text, pid)
        if checked is None:
            return {'checked': False}
        found = checked['hits']
        if not found:
            return {'checked': True, 'flagged': False, 'top_tier': None, 'hits': [], 'more': 0}
        from bps_redaction.checker.scan import DETECTOR_VERSION
        counts = dict(collections.Counter(h['type'] for h in found))
        top = min(h['tier'] for h in found)
        flag_id = store.add_flag(session_id=session_id, key_id=key.id, profiles=profiles,
                                 patient_hash=patient_hash(pid), top_tier=top, counts=counts,
                                 hits=found, ruleset=bps_redaction.RULESET,
                                 detector=DETECTOR_VERSION, content=text)
        shown = [{k: h[k] for k in ('type', 'tier', 'start', 'end')} for h in found[:MAX_HITS]]
        return {'checked': True, 'flagged': True, 'top_tier': top, 'hits': shown,
                'more': max(0, len(found) - MAX_HITS), 'flag_id': flag_id}

    # ── /v1/deredact ──────────────────────────────────────────────────────────

    @app.post('/v1/deredact')
    def deredact(body: DeredactIn, key: ApiKey = Depends(caller)):
        need(key, 'deredact')
        started = time.monotonic()
        session = own_session(key, body.session_id)      # reading back doesn't extend it
        restored = redactor.deredact(body.content, store.token_map(session.id))
        store.audit(key_id=key.id, action='deredact', outcome='ok',
                    counts={'restored': restored.restored, 'unresolved': restored.unresolved,
                            'swept': restored.swept},
                    duration_ms=int((time.monotonic() - started) * 1000))
        return {'content': restored.text, 'restored': restored.restored,
                'unresolved': restored.unresolved, 'swept': restored.swept}

    # ── sessions ──────────────────────────────────────────────────────────────

    @app.delete('/v1/sessions/{session_id}', status_code=204)
    def end_session(session_id: str, key: ApiKey = Depends(caller)):
        if not ({'redact', 'deredact'} & key.scopes):
            raise ApiError(403, 'forbidden')
        session = store.get_session(session_id)
        if session is None or session.key_id != key.id:
            raise ApiError(404, 'session_not_found')
        store.delete_session(session_id)
        store.audit(key_id=key.id, action='end_session', outcome='ok')
        return Response(status_code=204)

    # ── /v1/verify ────────────────────────────────────────────────────────────

    @app.post('/v1/verify')
    def verify(body: VerifyIn, key: ApiKey = Depends(caller)):
        need(key, 'verify')
        if body.patient_id is not None and not redactor.identifiers(body.patient_id):
            raise ApiError(503, 'identity_source_unavailable')
        checked = _check(redactor, _as_text(body.content), body.patient_id)
        if checked is None:
            raise ApiError(500, 'internal_error')
        store.audit(key_id=key.id, action='verify', outcome='ok',
                    patient_hash=patient_hash(body.patient_id),
                    counts=dict(collections.Counter(h['type'] for h in checked['hits'])))
        return {'hits': [{k: h[k] for k in ('type', 'tier', 'start', 'end')} for h in checked['hits']],
                'ruleset': bps_redaction.RULESET}

    # ── flags ─────────────────────────────────────────────────────────────────

    def _when(value: str | None, name: str):
        if value is None:
            return None
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            raise ApiError(422, f'bad_{name}')

    def _public(flag: dict) -> dict:
        out = dict(flag)
        for k in ('at', 'reviewed_at'):
            out[k] = _iso(out[k]) if out.get(k) else None
        return out

    @app.get('/v1/flags')
    def list_flags(since: str | None = None, until: str | None = None, tier: int | None = None,
                   type: str | None = None, disposition: str | None = None,
                   key_id: int | None = None, order: Literal['newest', 'oldest'] = 'newest',
                   limit: int = 50, offset: int = 0, key: ApiKey = Depends(caller)):
        need(key, 'flags')
        if disposition is not None and disposition not in DISPOSITIONS:
            raise ApiError(422, 'bad_disposition')
        rows = store.list_flags(since=_when(since, 'since'), until=_when(until, 'until'), tier=tier,
                                finding_type=type, disposition=disposition, key_id=key_id,
                                newest_first=order == 'newest', limit=max(1, min(limit, 500)),
                                offset=max(0, offset))
        return {'flags': [_public(r) for r in rows]}

    @app.get('/v1/flags/summary')
    def flag_summary(since: str | None = None, key: ApiKey = Depends(caller)):
        need(key, 'flags')
        return store.flag_summary(since=_when(since, 'since'))

    @app.get('/v1/flags/{flag_id}')
    def get_flag(flag_id: str, key: ApiKey = Depends(caller)):
        need(key, 'flags')
        flag = store.get_flag(flag_id)
        if flag is None:
            raise ApiError(404, 'flag_not_found')
        return _public(flag)

    @app.post('/v1/flags/{flag_id}/review')
    def review_flag(flag_id: str, body: ReviewIn, key: ApiKey = Depends(caller)):
        need(key, 'flags')
        if not store.review_flag(flag_id, body.disposition, body.reviewer or key.name, body.note):
            raise ApiError(404, 'flag_not_found')
        store.audit(key_id=key.id, action='review_flag', outcome=body.disposition)
        return _public(store.get_flag(flag_id))

    # ── health and info ───────────────────────────────────────────────────────

    @app.get('/v1/health')
    def health():
        cached = health_cache.get('source')
        if not cached or time.monotonic() - cached[1] > 60:
            source = redactor.source
            if source is None:
                state = 'none'
            else:
                try:
                    source.query('hcp_practice_contacts_fingerprint')
                    state = 'ok'
                except Exception:
                    state = 'unreachable'
            health_cache['source'] = cached = (state, time.monotonic())
        return {'status': 'ok' if cached[0] == 'ok' else 'degraded', 'source': cached[0]}

    @app.get('/v1/info')
    def info(key: ApiKey = Depends(caller)):
        return {'version': bps_redaction.__version__, 'ruleset': bps_redaction.RULESET,
                'source': settings.source, 'profiles': ['record', 'input', 'prompt'],
                'session_ttl_hours': settings.session_ttl / 3600,
                'session_cap_days': settings.session_cap / 86400,
                'max_bytes': settings.max_bytes, 'flag_days': settings.flag_days,
                'scopes': sorted(key.scopes)}

    return app
