# Miso Cloudflare Tunnel and Access

Miso's only remote hostname is `miso.jyjonline.com`. It uses a dedicated,
remotely managed Cloudflare Tunnel whose only published application route is
that hostname to the Miso HTTP origin. The existing `dmaga` tunnel and hostname
are a separate rollback path and must not be edited during rollout.

The Access application is self-hosted and covers the full hostname. Its policy
is default-deny with one Allow policy containing the same explicit Google email
addresses configured in `MISO_HOUSEHOLD_ALLOWED_EMAILS`; no broad domain or
Everyone selector is used. Google is the only enabled login method for the app.

The origin configuration in `/etc/miso/miso.env` contains:

```ini
MISO_ACCESS_TEAM_DOMAIN=https://sowe-tech.cloudflareaccess.com
MISO_ACCESS_AUDIENCE=<application AUD tag>
MISO_HOUSEHOLD_ALLOWED_EMAILS=<comma-separated allowed Google emails>
```

The application AUD is configuration, not a bearer secret. The tunnel token is
stored only in the root-readable systemd environment or credential file used by
the connector and is never stored in this repository.

`miso-cloudflared.service` reads `/etc/cloudflared/miso.token` through systemd's
credential directory and runs as a dynamic unprivileged user. The runtime
installer leaves the connector disabled when that root-only token file is
absent. The legacy `cloudflared.service` can therefore continue unchanged while
the Miso connector is deployed, verified, or rolled back independently.

## Request boundary

Cloudflare supplies `Cf-Access-Jwt-Assertion` after a successful Access policy
decision. Miso independently validates its signature against
`https://sowe-tech.cloudflareaccess.com/cdn-cgi/access/certs`, along with the
issuer, AUD, expiry, not-before/issued-at values when present, token type, and
email. The email must then pass Miso's household allowlist. Supplying only
`Cf-Access-Authenticated-User-Email` cannot authenticate a request.

Static PWA shell files remain public at the local origin so the LAN recovery UI
can load. Cloudflare Access protects the entire remote hostname before those
requests reach the tunnel. API responses remain `Cache-Control: no-store`, and
the service worker never caches API or authenticated requests.

## Validation and rollback

Acceptance uses one listed Google account and one unlisted account. The listed
account must reach `/api/identity` with its own normalized email; the unlisted
account and an assertion for a different Access application must receive a
denial. Direct origin probes with a forged identity header must receive `401`.
The connector must expose only the Miso hostname, while local SSH and
`http://miso.local` continue to work.

Rollback disables the dedicated Miso connector and removes only the Miso DNS
route after confirming LAN access. It does not alter the legacy `dmaga` tunnel,
the Pi's SSH service, or other local applications.
