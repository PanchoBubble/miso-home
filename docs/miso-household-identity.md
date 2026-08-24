# Miso household identity and sharing rules

Miso treats identity as an origin property, not as model-provided text. Web
requests resolve to a normalized, cryptographically verified email address
before they reach storage or tools. Voice input deliberately resolves to the
stable `household:voice` actor because speaker recognition is not enabled; it
must never inherit the identity of the last dashboard user or a name inferred
from a transcript. Background reconciliation uses `miso:system`.

## Actors and authorization

`MISO_DASHBOARD_EMAIL` names the local dashboard/bearer-token actor and defaults
to `local@miso.invalid`. The Cloudflare Access application policy is the sole
allowlist for remote household members.

The local dashboard authenticates with its existing bearer token and maps that
credential to `MISO_DASHBOARD_EMAIL`. Remote requests authenticate with the
`Cf-Access-Jwt-Assertion` application token. Miso validates its RS256 signature
against the team domain's rotating key set, exact issuer and application AUD,
time bounds, application-token type, and email. A verified email is normalized
and dynamically registered in SQLite for private ownership and audit
attribution. The unauthenticated convenience email header is never trusted.

## Authorization matrix

| Record | Voice actor | Owning web member | Other Access-authorized web member |
| --- | --- | --- | --- |
| Shared | Read/write | Read/write | Read/write |
| Private | Denied | Read/write | Denied |

Private ownership is always a normalized web email. Voice cannot create a
private record. Authorization predicates live in the SQLite store methods so a
future Calendar, list, reminder, memory, or notification view cannot bypass the
rule by omitting a browser-side filter.

Dashboard conversations and dashboard-created timers/reminders default to
private. Voice conversations and voice-created timers/reminders default to
shared. Shopping tools are explicitly shared today; the storage boundary also
supports private lists for the household-list UI.

## Attribution and migration

Schema version 3 adds household members, visibility, ownership, and actor
columns. Existing conversations, events, memories, scheduled items, shopping
lists, and shopping items migrate as shared records attributed to
`household:voice`, preserving their previous household-wide behavior.

Conversation events persist their actor. Every routing decision, provider
attempt, tool invocation, developer-mode change, conversation transition, and
scheduled-item reconciliation event records the responsible web, voice, or
system actor. The dashboard activity endpoint includes shared voice/system
activity plus the current member's own web activity, and excludes another
member's private web activity.
