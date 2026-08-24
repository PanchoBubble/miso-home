# Miso Google Calendar integration

Miso exposes five validated tools for listing calendars and reading, creating,
updating, or deleting events. Every web user has a separate local OAuth token.
Voice requests are disabled unless one authorized account is explicitly selected
as the household voice account.

The integration requests only Google identity, read-only calendar-list, and event
read/write scopes. OAuth access and refresh tokens are stored under
`/var/lib/miso/state/google-calendar` as mode `0600` files in a mode `0700`
directory. Tokens are used only in HTTPS authorization headers to Google's fixed
OAuth, user-info, and Calendar endpoints. They are never present in tool schemas,
arguments, results, audit records, prompts, or model requests.

## Google Cloud setup

1. Enable the Google Calendar API in the intended Google Cloud project.
2. Configure the OAuth consent screen. For an external test application, add each
   household Google account as a test user.
3. Add these scopes:
   - `openid`
   - `email`
   - `https://www.googleapis.com/auth/calendar.calendarlist.readonly`
   - `https://www.googleapis.com/auth/calendar.events`
4. Create a **Desktop app** OAuth client and download its JSON file.
5. Install it on the Pi without world access:

   ```bash
   sudo install -o root -g miso -m 0640 client_secret.json \
     /etc/miso/google-calendar-client.json
   ```

Google documents the current [Calendar scopes](https://developers.google.com/workspace/calendar/api/auth),
[desktop PKCE and loopback flow](https://developers.google.com/identity/protocols/oauth2/native-app),
and [offline token refresh](https://developers.google.com/identity/protocols/oauth2/web-server#offline).

## Authorize an account on the Pi

Use an SSH loopback tunnel so the browser callback reaches the helper on the Pi,
while the refresh token is written directly to its final local directory. Keep the
SSH command open, copy the printed URL into a browser on the workstation, and
complete Google consent using the same email as the Miso web identity.

```bash
ssh -L 8765:127.0.0.1:8765 pancho \
  'sudo -u miso env PYTHONPATH=/opt/miso/app/src \
   python3 -m miso.google_calendar_auth \
   --email juan@example.com --port 8765 --no-browser'
```

The helper uses a fresh CSRF state and PKCE verifier, verifies the Google account's
email, saves the token atomically, and prints only its local path. Run it once for
each household web user. Re-run it when Google reports that a grant was revoked or
expired.

## Enable the tools

Edit `/etc/miso/miso-calendar.env`:

```ini
MISO_GOOGLE_CALENDAR_ENABLED=true
MISO_GOOGLE_CALENDAR_CLIENT_PATH=/etc/miso/google-calendar-client.json
MISO_GOOGLE_CALENDAR_TOKEN_DIR=/var/lib/miso/state/google-calendar
MISO_GOOGLE_CALENDAR_DEFAULT_TIMEZONE=Europe/London
MISO_GOOGLE_CALENDAR_DEFAULT_ID=primary
MISO_GOOGLE_CALENDAR_VOICE_EMAIL=juan@example.com
```

Omit `MISO_GOOGLE_CALENDAR_VOICE_EMAIL` to keep Calendar unavailable to the
unidentified household voice actor. Restart the service; its pre-start check
validates configured paths before the runtime is allowed to start:

```bash
sudo systemctl restart miso
sudo systemctl status --no-pager miso
```

`calendar_list` reports each calendar ID, timezone, and effective access role.
Pass the chosen `calendar_id` to event tools, or rely on the configured default.
Timed events accept ISO 8601 values plus an IANA timezone. All-day events use
exclusive `YYYY-MM-DD` end dates. Recurrence accepts RFC 5545 `RRULE`, `EXRULE`,
`RDATE`, and `EXDATE` lines; `DTSTART` and `DTEND` remain in the event start/end
fields as required by the Calendar API.
