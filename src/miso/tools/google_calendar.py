"""Google Calendar tools with local-only OAuth credential handling."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from miso.identity import Actor, normalize_email
from miso.tools.base import ToolContext, ToolDefinition, ToolRegistry, ToolRejected


AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
CALENDAR_SCOPES = (
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events",
)
AUTHORIZATION_SCOPES = ("openid", "email", *CALENDAR_SCOPES)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class GoogleCalendarError(RuntimeError):
    """Base error for bounded Google Calendar failures."""


class GoogleAuthorizationRequired(ToolRejected):
    """The actor must authorize or reauthorize Google Calendar locally."""


@dataclass(frozen=True, slots=True)
class GoogleOAuthClient:
    client_id: str
    client_secret: str = field(repr=False)

    @classmethod
    def load(cls, path: Path) -> "GoogleOAuthClient":
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise GoogleCalendarError(
                    "Google OAuth client path is not a regular file"
                )
            if stat.S_IMODE(metadata.st_mode) & 0o007:
                raise GoogleCalendarError(
                    "Google OAuth client file must not be readable by other users"
                )
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise GoogleCalendarError("Google OAuth client file is unreadable") from error
        if not isinstance(raw, Mapping):
            raise GoogleCalendarError("Google OAuth client file is invalid")
        section = raw.get("installed") or raw.get("web")
        if not isinstance(section, Mapping):
            raise GoogleCalendarError(
                "Google OAuth client file must contain installed or web credentials"
            )
        client_id = section.get("client_id")
        client_secret = section.get("client_secret")
        if (
            not isinstance(client_id, str)
            or not client_id.endswith(".apps.googleusercontent.com")
            or not isinstance(client_secret, str)
            or not client_secret
        ):
            raise GoogleCalendarError("Google OAuth client credentials are invalid")
        for key, expected in (
            ("auth_uri", AUTHORIZATION_ENDPOINT),
            ("token_uri", TOKEN_ENDPOINT),
        ):
            configured = section.get(key)
            if configured is not None and configured != expected:
                raise GoogleCalendarError(f"Google OAuth {key} is not trusted")
        return cls(client_id, client_secret)


@dataclass(frozen=True, slots=True)
class GoogleToken:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float
    scopes: tuple[str, ...]
    token_type: str = "Bearer"

    @classmethod
    def from_response(
        cls,
        response: Mapping[str, object],
        *,
        now: float,
        previous: "GoogleToken | None" = None,
    ) -> "GoogleToken":
        access_token = response.get("access_token")
        refresh_token = response.get("refresh_token") or (
            previous.refresh_token if previous is not None else None
        )
        token_type = response.get("token_type", "Bearer")
        expires_in = response.get("expires_in")
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(refresh_token, str)
            or not refresh_token
            or token_type != "Bearer"
            or not isinstance(expires_in, (int, float))
            or isinstance(expires_in, bool)
            or not 1 <= float(expires_in) <= 86400
        ):
            raise GoogleCalendarError("Google returned an invalid OAuth token response")
        raw_scope = response.get("scope")
        if isinstance(raw_scope, str):
            scopes = tuple(sorted(set(raw_scope.split())))
        elif previous is not None:
            scopes = previous.scopes
        else:
            scopes = AUTHORIZATION_SCOPES
        if not set(CALENDAR_SCOPES).issubset(scopes):
            raise GoogleAuthorizationRequired(
                "Google Calendar authorization is missing required scopes"
            )
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=now + float(expires_in),
            scopes=scopes,
            token_type="Bearer",
        )


class GoogleTokenStore:
    """Store one restrictive local token file per authenticated web account."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, email: str) -> Path:
        normalized = normalize_email(email)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def load(self, email: str) -> GoogleToken:
        normalized = normalize_email(email)
        path = self.path_for(normalized)
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise GoogleCalendarError("Google token path is not a regular file")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise GoogleCalendarError("Google token file permissions are too broad")
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise GoogleAuthorizationRequired(
                "Google Calendar is not authorized for this user"
            ) from error
        except OSError as error:
            raise GoogleCalendarError("Google token file is unreadable") from error
        except json.JSONDecodeError as error:
            raise GoogleCalendarError("Google token file is invalid") from error
        if not isinstance(raw, Mapping) or raw.get("email") != normalized:
            raise GoogleCalendarError("Google token file identity does not match")
        try:
            access_token = raw["access_token"]
            refresh_token = raw["refresh_token"]
            expires_at = raw["expires_at"]
            scopes = raw["scopes"]
            token_type = raw.get("token_type", "Bearer")
            if (
                not isinstance(access_token, str)
                or not access_token
                or not isinstance(refresh_token, str)
                or not refresh_token
                or not isinstance(expires_at, (int, float))
                or isinstance(expires_at, bool)
                or not isinstance(scopes, list)
                or not all(isinstance(scope, str) for scope in scopes)
                or token_type != "Bearer"
            ):
                raise ValueError
        except (KeyError, ValueError, TypeError) as error:
            raise GoogleCalendarError("Google token file is invalid") from error
        if not set(CALENDAR_SCOPES).issubset(scopes):
            raise GoogleAuthorizationRequired(
                "Google Calendar authorization is missing required scopes"
            )
        return GoogleToken(
            access_token,
            refresh_token,
            float(expires_at),
            tuple(scopes),
            str(token_type),
        )

    def save(self, email: str, token: GoogleToken) -> Path:
        normalized = normalize_email(email)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        path = self.path_for(normalized)
        payload = json.dumps(
            {
                "email": normalized,
                "access_token": token.access_token,
                "refresh_token": token.refresh_token,
                "expires_at": token.expires_at,
                "scopes": list(token.scopes),
                "token_type": token.token_type,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=self.root
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise
        return path

    def delete(self, email: str) -> None:
        self.path_for(email).unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    status: int
    body: bytes


class HTTPTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HTTPResponse: ...


class UrllibTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HTTPResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                content = response.read(MAX_RESPONSE_BYTES + 1)
                status_code = response.status
        except HTTPError as error:
            content = error.read(MAX_RESPONSE_BYTES + 1)
            status_code = error.code
        except (OSError, URLError) as error:
            raise GoogleCalendarError("Google Calendar network request failed") from error
        if len(content) > MAX_RESPONSE_BYTES:
            raise GoogleCalendarError("Google Calendar response exceeded the size limit")
        return HTTPResponse(status_code, content)


class GoogleOAuthSession:
    def __init__(
        self,
        client: GoogleOAuthClient,
        tokens: GoogleTokenStore,
        *,
        transport: HTTPTransport | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.tokens = tokens
        self.transport = transport or UrllibTransport()
        self._now = now

    def authorization_url(
        self,
        email: str,
        redirect_uri: str,
        state: str,
        code_challenge: str,
    ) -> str:
        return AUTHORIZATION_ENDPOINT + "?" + urlencode(
            {
                "client_id": self.client.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(AUTHORIZATION_SCOPES),
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
                "login_hint": normalize_email(email),
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )

    def exchange_code(
        self,
        email: str,
        code: str,
        redirect_uri: str,
        code_verifier: str,
        *,
        timeout: float = 15,
    ) -> GoogleToken:
        response = self._token_request(
            {
                "client_id": self.client.client_id,
                "client_secret": self.client.client_secret,
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout,
        )
        token = GoogleToken.from_response(response, now=self._now())
        actual_email = self.userinfo_email(token.access_token, timeout=timeout)
        expected_email = normalize_email(email)
        if actual_email != expected_email:
            raise GoogleAuthorizationRequired(
                "Google account does not match the authenticated Miso user"
            )
        self.tokens.save(expected_email, token)
        return token

    def userinfo_email(self, access_token: str, *, timeout: float) -> str:
        response = self.transport.request(
            "GET",
            USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
            body=None,
            timeout=timeout,
        )
        payload = _json_object(response.body)
        if response.status != 200 or payload.get("email_verified") is not True:
            raise GoogleAuthorizationRequired("Google account identity was not verified")
        email = payload.get("email")
        if not isinstance(email, str):
            raise GoogleAuthorizationRequired("Google account identity was not returned")
        return normalize_email(email)

    def access_token(self, email: str, *, timeout: float, force: bool = False) -> str:
        normalized = normalize_email(email)
        token = self.tokens.load(normalized)
        if not force and token.expires_at > self._now() + 60:
            return token.access_token
        try:
            response = self._token_request(
                {
                    "client_id": self.client.client_id,
                    "client_secret": self.client.client_secret,
                    "refresh_token": token.refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout,
            )
            refreshed = GoogleToken.from_response(
                response, now=self._now(), previous=token
            )
        except GoogleAuthorizationRequired:
            self.tokens.delete(normalized)
            raise
        self.tokens.save(normalized, refreshed)
        return refreshed.access_token

    def _token_request(
        self, parameters: Mapping[str, str], timeout: float
    ) -> Mapping[str, object]:
        response = self.transport.request(
            "POST",
            TOKEN_ENDPOINT,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=urlencode(parameters).encode("ascii"),
            timeout=timeout,
        )
        payload = _json_object(response.body)
        if response.status != 200:
            if payload.get("error") in {
                "invalid_grant",
                "invalid_client",
                "unauthorized_client",
            }:
                raise GoogleAuthorizationRequired(
                    "Google Calendar authorization expired; reconnect the account"
                )
            raise GoogleCalendarError(
                f"Google OAuth request failed with status {response.status}"
            )
        return payload


@dataclass(frozen=True, slots=True)
class GoogleCalendarConfig:
    client_path: Path
    token_dir: Path
    default_timezone: str = "Europe/London"
    default_calendar_id: str = "primary"
    voice_account_email: str | None = None

    def validate(self) -> None:
        if not self.client_path.is_absolute() or not self.token_dir.is_absolute():
            raise ValueError("Google Calendar credential paths must be absolute")
        _timezone(self.default_timezone)
        _bounded_text(self.default_calendar_id, "calendar_id", 1024)
        if self.voice_account_email is not None:
            normalize_email(self.voice_account_email)


class GoogleCalendarAdapter:
    def __init__(
        self,
        config: GoogleCalendarConfig,
        *,
        transport: HTTPTransport | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        config.validate()
        self.config = config
        self.oauth = GoogleOAuthSession(
            GoogleOAuthClient.load(config.client_path),
            GoogleTokenStore(config.token_dir),
            transport=transport,
            now=now,
        )
        self.transport = self.oauth.transport

    def account_for(self, actor: Actor) -> str:
        if actor.is_web and actor.email is not None:
            return actor.email
        if actor.source == "voice" and self.config.voice_account_email is not None:
            return normalize_email(self.config.voice_account_email)
        raise GoogleAuthorizationRequired(
            "Google Calendar requires an authenticated web user or configured voice account"
        )

    def request(
        self,
        actor: Actor,
        context: ToolContext,
        method: str,
        path: str,
        *,
        query: Mapping[str, object] | None = None,
        body: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Mapping[str, object]:
        account = self.account_for(actor)
        context.raise_if_cancelled()
        url = CALENDAR_API_BASE + path
        if query:
            encoded = urlencode(
                [(key, str(value).lower() if isinstance(value, bool) else str(value))
                 for key, value in query.items() if value is not None]
            )
            url += "?" + encoded
        payload = None
        request_headers = {"Accept": "application/json"}
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json; charset=utf-8"
        if headers:
            request_headers.update(headers)
        for attempt in range(2):
            token = self.oauth.access_token(
                account,
                timeout=max(0.1, context.remaining_seconds()),
                force=attempt == 1,
            )
            request_headers["Authorization"] = f"Bearer {token}"
            response = self.transport.request(
                method,
                url,
                headers=request_headers,
                body=payload,
                timeout=max(0.1, context.remaining_seconds()),
            )
            context.raise_if_cancelled()
            if response.status == 401 and attempt == 0:
                continue
            if response.status == 204:
                return {}
            result = _json_object(response.body)
            if 200 <= response.status < 300:
                return result
            if response.status == 401:
                raise GoogleAuthorizationRequired(
                    "Google Calendar authorization is invalid"
                )
            if response.status == 403:
                raise ToolRejected(
                    "Google Calendar denied the operation or its quota was exceeded"
                )
            if response.status == 404:
                raise ToolRejected("Google Calendar resource was not found")
            if response.status == 412:
                raise ToolRejected("Google Calendar event changed; retry with fresh data")
            if 400 <= response.status < 500:
                raise ToolRejected(
                    f"Google Calendar rejected the request with status {response.status}"
                )
            raise GoogleCalendarError(
                f"Google Calendar request failed with status {response.status}"
            )
        raise GoogleAuthorizationRequired("Google Calendar authorization failed")

    def calendar_list(
        self, arguments: Mapping[str, object], context: ToolContext
    ) -> Mapping[str, object]:
        payload = self.request(
            context.actor,
            context,
            "GET",
            "/users/me/calendarList",
            query={"maxResults": int(arguments.get("max_results", 100))},
        )
        items = payload.get("items", [])
        if not isinstance(items, list):
            raise GoogleCalendarError("Google Calendar returned an invalid calendar list")
        return {
            "calendars": [
                {
                    "id": item.get("id"),
                    "name": item.get("summaryOverride") or item.get("summary"),
                    "time_zone": item.get("timeZone"),
                    "primary": bool(item.get("primary", False)),
                    "selected": bool(item.get("selected", False)),
                    "access_role": item.get("accessRole"),
                    "writable": item.get("accessRole")
                    in {"writerWithoutPrivateAccess", "writer", "owner"},
                }
                for item in items
                if isinstance(item, Mapping)
                and isinstance(item.get("id"), str)
                and not item.get("deleted", False)
            ]
        }

    def event_list(
        self, arguments: Mapping[str, object], context: ToolContext
    ) -> Mapping[str, object]:
        minimum = _rfc3339(arguments["time_min"], "time_min")
        maximum = _rfc3339(arguments["time_max"], "time_max")
        if datetime.fromisoformat(minimum) >= datetime.fromisoformat(maximum):
            raise ToolRejected("time_max must be after time_min")
        calendar_id = self._calendar_id(arguments)
        payload = self.request(
            context.actor,
            context,
            "GET",
            f"/calendars/{quote(calendar_id, safe='')}/events",
            query={
                "timeMin": minimum,
                "timeMax": maximum,
                "singleEvents": bool(arguments.get("expand_recurring", True)),
                "orderBy": "startTime"
                if bool(arguments.get("expand_recurring", True))
                else None,
                "maxResults": int(arguments.get("max_results", 25)),
                "q": arguments.get("query"),
                "showDeleted": False,
            },
        )
        items = payload.get("items", [])
        if not isinstance(items, list):
            raise GoogleCalendarError("Google Calendar returned an invalid event list")
        return {
            "calendar_id": calendar_id,
            "time_zone": payload.get("timeZone"),
            "events": [_public_event(item) for item in items if isinstance(item, Mapping)],
        }

    def event_create(
        self, arguments: Mapping[str, object], context: ToolContext
    ) -> Mapping[str, object]:
        calendar_id = self._calendar_id(arguments)
        event = self._event_changes(arguments, require_times=True)
        payload = self.request(
            context.actor,
            context,
            "POST",
            f"/calendars/{quote(calendar_id, safe='')}/events",
            body=event,
        )
        return {"calendar_id": calendar_id, "event": _public_event(payload)}

    def event_update(
        self, arguments: Mapping[str, object], context: ToolContext
    ) -> Mapping[str, object]:
        calendar_id = self._calendar_id(arguments)
        event_id = _bounded_text(arguments["event_id"], "event_id", 1024)
        path = (
            f"/calendars/{quote(calendar_id, safe='')}/events/"
            f"{quote(event_id, safe='')}"
        )
        existing = self.request(context.actor, context, "GET", path)
        changes = self._event_changes(arguments, require_times=False)
        if not changes:
            raise ToolRejected("at least one event field must be updated")
        writable = _writable_event(existing)
        writable.update(changes)
        etag = existing.get("etag")
        conditional = {"If-Match": etag} if isinstance(etag, str) else None
        payload = self.request(
            context.actor,
            context,
            "PUT",
            path,
            body=writable,
            headers=conditional,
        )
        return {"calendar_id": calendar_id, "event": _public_event(payload)}

    def event_delete(
        self, arguments: Mapping[str, object], context: ToolContext
    ) -> Mapping[str, object]:
        calendar_id = self._calendar_id(arguments)
        event_id = _bounded_text(arguments["event_id"], "event_id", 1024)
        self.request(
            context.actor,
            context,
            "DELETE",
            f"/calendars/{quote(calendar_id, safe='')}/events/"
            f"{quote(event_id, safe='')}",
        )
        return {"calendar_id": calendar_id, "event_id": event_id, "deleted": True}

    def _calendar_id(self, arguments: Mapping[str, object]) -> str:
        return _bounded_text(
            arguments.get("calendar_id", self.config.default_calendar_id),
            "calendar_id",
            1024,
        )

    def _event_changes(
        self, arguments: Mapping[str, object], *, require_times: bool
    ) -> dict[str, object]:
        changes: dict[str, object] = {}
        for argument, field_name, maximum in (
            ("summary", "summary", 1000),
            ("description", "description", 8192),
            ("location", "location", 1000),
        ):
            if argument in arguments:
                changes[field_name] = _bounded_text(
                    arguments[argument], argument, maximum, allow_empty=True
                )
        has_start = "start" in arguments
        has_end = "end" in arguments
        if has_start != has_end:
            raise ToolRejected("start and end must be updated together")
        if require_times and not (has_start and has_end):
            raise ToolRejected("start and end are required")
        if has_start:
            all_day = bool(arguments.get("all_day", False))
            time_zone = str(
                arguments.get("time_zone", self.config.default_timezone)
            )
            _timezone(time_zone)
            start, end = _event_times(
                arguments["start"], arguments["end"], all_day, time_zone
            )
            changes["start"] = start
            changes["end"] = end
        elif "all_day" in arguments or "time_zone" in arguments:
            raise ToolRejected("all_day and time_zone require start and end")
        if "recurrence" in arguments:
            recurrence = arguments["recurrence"]
            assert isinstance(recurrence, (list, tuple))
            changes["recurrence"] = [_recurrence(value) for value in recurrence]
        return changes


def google_calendar_tool_definitions(
    adapter: GoogleCalendarAdapter,
) -> tuple[ToolDefinition, ...]:
    text = {"type": "string", "minLength": 1, "maxLength": 1024}
    timestamp = {"type": "string", "minLength": 10, "maxLength": 64}
    optional_text = {"type": "string", "maxLength": 8192}
    recurrence = {
        "type": "array",
        "items": {"type": "string", "minLength": 7, "maxLength": 2048},
        "maxItems": 32,
        "uniqueItems": True,
    }
    event_fields = {
        "calendar_id": text,
        "summary": {"type": "string", "minLength": 1, "maxLength": 1000},
        "description": optional_text,
        "location": {"type": "string", "maxLength": 1000},
        "start": timestamp,
        "end": timestamp,
        "time_zone": {"type": "string", "minLength": 1, "maxLength": 100},
        "all_day": {"type": "boolean"},
        "recurrence": recurrence,
    }
    return (
        ToolDefinition(
            "calendar_list",
            "List available Google calendars and access levels; lista calendarios de Google",
            _object_schema(
                {"max_results": {"type": "integer", "minimum": 1, "maximum": 250}}
            ),
            adapter.calendar_list,
            timeout_seconds=20,
        ),
        ToolDefinition(
            "calendar_event_list",
            "Read Google Calendar events in a time range; consulta eventos y agenda",
            _object_schema(
                {
                    "calendar_id": text,
                    "time_min": timestamp,
                    "time_max": timestamp,
                    "query": {"type": "string", "minLength": 1, "maxLength": 500},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
                    "expand_recurring": {"type": "boolean"},
                },
                ("time_min", "time_max"),
            ),
            adapter.event_list,
            timeout_seconds=20,
        ),
        ToolDefinition(
            "calendar_event_create",
            "Create a timezone-aware Google Calendar event or recurring series; "
            "crea un evento o cita",
            _object_schema(event_fields, ("summary", "start", "end")),
            adapter.event_create,
            timeout_seconds=20,
        ),
        ToolDefinition(
            "calendar_event_update",
            "Update a Google Calendar event or recurring series; actualiza un evento o cita",
            _object_schema({"event_id": text, **event_fields}, ("event_id",)),
            adapter.event_update,
            timeout_seconds=20,
        ),
        ToolDefinition(
            "calendar_event_delete",
            "Delete a Google Calendar event or recurring series; elimina un evento o cita",
            _object_schema({"calendar_id": text, "event_id": text}, ("event_id",)),
            adapter.event_delete,
            timeout_seconds=20,
        ),
    )


def register_google_calendar_tools(
    registry: ToolRegistry,
    config: GoogleCalendarConfig,
    *,
    transport: HTTPTransport | None = None,
    now: Callable[[], float] = time.time,
) -> GoogleCalendarAdapter:
    adapter = GoogleCalendarAdapter(config, transport=transport, now=now)
    for definition in google_calendar_tool_definitions(adapter):
        registry.register(definition)
    return adapter


def _object_schema(
    properties: Mapping[str, object], required: tuple[str, ...] = ()
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


def _json_object(body: bytes) -> Mapping[str, object]:
    if not body:
        return {}
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GoogleCalendarError("Google returned an invalid JSON response") from error
    if not isinstance(value, Mapping):
        raise GoogleCalendarError("Google returned an invalid response object")
    return value


def _bounded_text(
    value: object, name: str, maximum: int, *, allow_empty: bool = False
) -> str:
    if not isinstance(value, str):
        raise ToolRejected(f"{name} must be a string")
    text = value.strip()
    if (not text and not allow_empty) or len(text) > maximum or any(
        ord(character) < 32 for character in text
    ):
        raise ToolRejected(f"{name} is invalid")
    return text


def _timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ToolRejected("time_zone must be a valid IANA timezone") from error


def _rfc3339(value: object, name: str) -> str:
    text = _bounded_text(value, name, 64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ToolRejected(f"{name} must be a valid RFC3339 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ToolRejected(f"{name} must include a timezone offset")
    return parsed.isoformat()


def _event_times(
    start_value: object,
    end_value: object,
    all_day: bool,
    time_zone: str,
) -> tuple[dict[str, str], dict[str, str]]:
    start_text = _bounded_text(start_value, "start", 64)
    end_text = _bounded_text(end_value, "end", 64)
    if all_day:
        try:
            start_date = date.fromisoformat(start_text)
            end_date = date.fromisoformat(end_text)
        except ValueError as error:
            raise ToolRejected("all-day start and end must be YYYY-MM-DD dates") from error
        if end_date <= start_date:
            raise ToolRejected("all-day end date must be after start date")
        return {"date": start_date.isoformat()}, {"date": end_date.isoformat()}
    try:
        start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ToolRejected("event start and end must be ISO 8601 date-times") from error
    zone = _timezone(time_zone)
    comparable_start = start.replace(tzinfo=zone) if start.tzinfo is None else start
    comparable_end = end.replace(tzinfo=zone) if end.tzinfo is None else end
    if comparable_end.astimezone(ZoneInfo("UTC")) <= comparable_start.astimezone(
        ZoneInfo("UTC")
    ):
        raise ToolRejected("event end must be after start")
    return (
        {"dateTime": start.isoformat(), "timeZone": time_zone},
        {"dateTime": end.isoformat(), "timeZone": time_zone},
    )


def _recurrence(value: object) -> str:
    text = _bounded_text(value, "recurrence", 2048)
    if not text.startswith(("RRULE:", "EXRULE:", "RDATE:", "EXDATE:")):
        raise ToolRejected(
            "recurrence entries must be RFC5545 RRULE, EXRULE, RDATE, or EXDATE lines"
        )
    return text


def _public_event(event: Mapping[str, object]) -> dict[str, object]:
    return {
        key: event[key]
        for key in (
            "id",
            "status",
            "summary",
            "description",
            "location",
            "start",
            "end",
            "recurrence",
            "recurringEventId",
            "htmlLink",
            "created",
            "updated",
        )
        if key in event
    }


def _writable_event(event: Mapping[str, object]) -> dict[str, object]:
    return {
        key: event[key]
        for key in (
            "summary",
            "description",
            "location",
            "start",
            "end",
            "recurrence",
            "attendees",
            "reminders",
            "visibility",
            "transparency",
        )
        if key in event
    }
