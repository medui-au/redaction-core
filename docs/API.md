# BPS Redaction Service: client guide

## Read this first: restore within 24 hours

The service keeps each conversation's token map (which token stands for
which original) for **24 hours after your last `/v1/redact`** on that
session, and **never more than 7 days** after the session began. After
that the map is deleted and `/v1/deredact` answers `410 session_expired`.
Nothing can bring it back.

So: de-redact replies as they arrive, and **store restored text, never
tokens**. When a conversation is finished, end it with
`DELETE /v1/sessions/{id}` so the map goes at once.

## The flow

```
POST /v1/redact     your text + patient id   → redacted text + session_id
   … send the redacted text to the model …
POST /v1/deredact   the model's reply + session_id → restored text
DELETE /v1/sessions/{session_id}               → the map is purged
```

Every request carries `Authorization: Bearer <key>`. Keys are issued by an
administrator on the `/admin` API keys page (or with `bps-redact keys add`),
with scopes: `redact`, `deredact`, `verify`, `flags`.

## POST /v1/redact

```json
{
  "profiles": ["input", "prompt"],
  "patient_id": 12345,
  "content": "text",
  "session_id": "optional: continue a conversation"
}
```

| profile | for | content |
|---|---|---|
| `record` | structured EHR data | JSON object or list; needs `patient_id` |
| `input` | what a person typed or dictated: adds practitioner names | text, or an object of fields |
| `prompt` | an assembled prompt, template included | text |

Profiles run in the order given. Leave out `patient_id` for text about no
particular patient: only the passes that need no patient record run
(practitioners, other people's contact details and addresses, the
practice's own details, titled names).

Pass the `session_id` you got back on every later call in the same
conversation: the same name then gets the same token, and the map keeps
growing. Each call moves the 24-hour limit forward.

```json
{
  "content": "[Redacted Patient: PT-43391ac7ced4] saw Dr [Redacted HCP: HP-6dfe39ae2df7] …",
  "session_id": "k3J9…",
  "expires_at": "2026-10-08T19:37:46Z",
  "summary": {"patient": 2, "hcp": 1},
  "identifiers_loaded": true,
  "profiles": ["input", "prompt"],
  "ruleset": "medui-adr067",
  "flags": {"checked": true, "flagged": false, "top_tier": null, "hits": [], "more": 0}
}
```

### The flag report

Every `/v1/redact` checks its own output for what the rules couldn't handle,
and says so in `flags`. When something is found:

```json
"flags": {"checked": true, "flagged": true, "top_tier": 1,
          "hits": [{"type": "practice_contact_name", "tier": 1, "start": 15, "end": 33}],
          "more": 0, "flag_id": "f_Ovs_Boha2LaKAf2y"}
```

Offsets point into the `content` returned (for structured content, into its
JSON text), so you can highlight them; matched text is never echoed. At most
20 hits are listed, with `more` counting the rest. Tiers: **0** = this patient's
own identifier, **1** = a named practitioner or a practice detail, **2** = a
shape that may be anyone's (a phone number, an email, a titled name…).

What a flag means is your call: send anyway, block, or ask the user. The
service only reports. `{"checked": false}` means the checker itself failed; the
redaction still stands. Treat checking as part of the workflow, not an extra:
rule-based redaction misses things, and this is how you find out.

Tokens look like `[Redacted <Label>: XX-<hex>]` and are restored. Some text is
replaced with fixed text that is **not** restored: `[Redacted Address]` (an
address not the patient's own), `[Redacted DOB]` (someone else's date of
birth), `[Link removed]`.

## POST /v1/deredact

```json
{"content": "the model's reply", "session_id": "k3J9…"}
```
```json
{"content": "restored text", "restored": 3, "unresolved": 0, "swept": 0}
```

A token the model mangled, or one not from this session, is replaced with
`(redacted)` (`swept`): you are never handed a raw token. Reading back does
not extend the session.

## POST /v1/verify (scope `verify`)

Checks text that didn't come through `/v1/redact`: `{"content": "…", "patient_id": 12345}`
→ `{"hits": [...], "ruleset": "…"}`. Nothing is stored.

## Flags (scope `flags`: reviewers)

| | |
|---|---|
| `GET /v1/flags` | `since`, `until` (ISO), `tier`, `type`, `disposition`, `key_id`, `order=newest\|oldest`, `limit`, `offset` |
| `GET /v1/flags/summary` | counts by top tier, type, day and disposition |
| `GET /v1/flags/{id}` | one flag; `content` holds the flagged text **while its session lives**, then `null` |
| `POST /v1/flags/{id}/review` | `{"disposition": "true_leak" \| "false_positive" \| "acceptable" \| "unreviewed", "note": "…"}` |

A flag's types, tiers and offsets are kept 90 days. The flagged content is
encrypted and goes with its session (24 hours), like the token map.

## Errors

| status | error | meaning |
|---|---|---|
| 401 | `unauthorised` | missing or unknown key |
| 403 | `forbidden` | the key lacks the scope |
| 404 | `session_not_found` | no such session for this key |
| 410 | `session_expired` | past its 24 hours (or 7 days): restore no more |
| 413 | `request_too_large` | over the size cap (2 MB by default) |
| 422 | `invalid_request` and others | the request is malformed; the text is never echoed back |
| 429 | `rate_limited` | too many requests for this key |
| 503 | `identity_source_unavailable` | the patient's identifiers couldn't be loaded: **nothing is returned, don't send the text anywhere** |
| 500 | `internal_error` | redaction failed: nothing is returned |

## What the service keeps

- **For at most 24 hours after the last use (7 days at most), encrypted:**
  each token's original, for the sessions that used it. That is the only
  health information held. Text itself is never stored.
- **Longer, counts only:** an audit row per call (time, key, profiles, a
  keyed hash of the patient id, how many of each kind of token, duration,
  outcome). No text, no tokens, no originals.
- The service logs counts and error types, never request bodies.

## Limits, honestly

Redaction is rule-based and best-effort. The rules were tuned on one
practice's real text, for Australian formats (Medicare numbers, AU phone
numbers and addresses) and Best Practice data. They will miss things.
Check what you send.

Anyone holding a `redact` key can test whether a string is a patient's
identifier by watching whether it becomes a token. Keys, scopes, rate limits
and the audit are the controls; issue keys accordingly.
