# Open-Meteo weather tool

Miso registers one `weather_get` tool for current conditions and a one-to-seven
day forecast. The local model chooses and validates the tool; the tool sends a
bounded HTTPS request to the fixed Open-Meteo geocoding and forecast endpoints.
It does not accept caller-supplied URLs or require an API credential.

The tool accepts a city or place name, `metric` or `imperial` units, an English
or Spanish response language, and the number of forecast days. Results include
a concise speakable summary, structured current/daily values, source attribution,
and coordinates rounded to four decimal places. Responses are cached in memory
for ten minutes. Location arguments are redacted from the durable tool audit.

Set an optional household default in root-owned `/etc/miso/miso.env`:

```bash
MISO_WEATHER_DEFAULT_LOCATION=London
```

When no default is configured, the tool schema requires a location and Miso
should ask for one. Restart `miso.service` after changing the setting because
tool schemas are assembled at startup.

The hosted Open-Meteo API is an online dependency. An outage produces a bounded
tool rejection; it does not fall through to fabricated model weather. API data
is attributed as `Weather data by Open-Meteo.com` with a link to
<https://open-meteo.com/>. The free hosted API is intended for non-commercial
use and asks users exceeding 10,000 requests per day to contact Open-Meteo.
