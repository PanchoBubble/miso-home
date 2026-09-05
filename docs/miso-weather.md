# Open-Meteo weather tool

Miso registers `weather_get` for current conditions and a one-to-seven day
forecast, and `weather_set_home` for changing which place counts as home. The
local model chooses and validates the tool; the tool sends a bounded HTTPS
request to the fixed Open-Meteo geocoding and forecast endpoints. It does not
accept caller-supplied URLs or require an API credential.

The tool accepts a city or place name, `metric` or `imperial` units, an English
or Spanish response language, and the number of forecast days. Results include
a concise speakable summary, structured current/daily values, source attribution,
and coordinates rounded to four decimal places. Responses are cached in memory
for ten minutes. Location arguments are redacted from the durable tool audit.

The spoken summary is the answer somebody actually asked for: current
conditions and temperature, whether it is raining now or likely to, and today's
high and low. Attribution stays in the structured output rather than being read
aloud every time. Rain is called likely at 60% or more and possible at 30% or
more; below that the day is reported as dry.

## Setting the household location

The location lives in the database, in `household_settings` under
`weather.location`, and can be changed while Miso is running:

- **Dashboard.** System panel, "Weather location". Type a place and save, or
  clear it. `POST /api/weather/location` with `{"location": "Barcelona"}`, or
  `{"location": null}` to clear.
- **Voice.** "Set the weather location to Barcelona", "change the weather to
  Madrid", "cambia el tiempo de casa a Sevilla". These phrasings are matched
  deterministically by the fast lane; the model can also call
  `weather_set_home` directly.

Both routes go through the same validated, audited `weather_set_home` tool,
which resolves the place before storing it, so a typo is refused immediately
rather than becoming a poller that fails every fifteen minutes. What gets
stored is the resolved place name, which is also the label on the screen.

`weather_set_home` is deliberately not available to the small tool picker: a
household setting should not be inferred from an utterance that already missed
the deterministic parsers.

Changing the location drops the stored snapshot and triggers an immediate
re-poll, so the face panel shows the new place within a second or two. No
restart is needed, and `weather_get`'s schema stops requiring an explicit
location as soon as a home exists.

`MISO_WEATHER_DEFAULT_LOCATION` only seeds the value: on a database with no
stored location, the env value is used. Once anyone sets a location, the stored
one wins and survives restarts.

## Polling and the companion panel

When a default location is configured, Miso polls it once at startup and then
every `MISO_WEATHER_POLL_SECONDS` (900 by default, minimum 60, `0` disables
polling). The snapshot is held in memory only: a restart simply polls again.

The polled snapshot is what answers a voice question about the household
location, so "is it going to rain" does not wait on the network. A question
about anywhere else, or one asking for more forecast days than the poller
carries, still makes a live request with the usual ten-minute cache. A snapshot
older than three poll intervals stops standing in for a live lookup.

Every successful poll publishes a `weather_update` live event and appears under
`weather` in `/api/status`, which is what the companion screen's always-on
panel draws. A failed poll keeps the last good snapshot, records the reason in
`/api/status`, and the panel dims once the reading passes 45 minutes old.

Set an optional household default in root-owned `/etc/miso/miso.env`:

```bash
MISO_WEATHER_DEFAULT_LOCATION=London
MISO_WEATHER_UNITS=metric          # or imperial
MISO_WEATHER_LANGUAGE=en           # or es; the language of the screen panel
MISO_WEATHER_POLL_SECONDS=900      # 0 disables polling
```

When no location is configured anywhere, `weather_get` requires an explicit
one and Miso asks for a place. Changing `MISO_WEATHER_UNITS`,
`MISO_WEATHER_LANGUAGE`, or `MISO_WEATHER_POLL_SECONDS` still needs a
`miso.service` restart; the location itself does not.

The hosted Open-Meteo API is an online dependency. An outage produces a bounded
tool rejection; it does not fall through to fabricated model weather. API data
is attributed as `Weather data by Open-Meteo.com` with a link to
<https://open-meteo.com/>. The free hosted API is intended for non-commercial
use and asks users exceeding 10,000 requests per day to contact Open-Meteo.
