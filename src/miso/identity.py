"""Household actors and server-side record visibility rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


class IdentityError(ValueError):
    """Raised when an identity or visibility rule is invalid."""


def normalize_email(value: str) -> str:
    email = value.strip().casefold()
    if (
        len(email) > 254
        or email.count("@") != 1
        or any(character.isspace() for character in email)
    ):
        raise IdentityError("household email address is invalid")
    local, domain = email.split("@", 1)
    if not local or not domain or domain.startswith(".") or domain.endswith("."):
        raise IdentityError("household email address is invalid")
    return email


@dataclass(frozen=True, slots=True)
class Actor:
    """An authenticated web member or the explicitly unidentified voice actor."""

    actor_id: str
    source: str
    email: str | None = None

    def __post_init__(self) -> None:
        if self.source not in {"web", "voice", "system"}:
            raise IdentityError("actor source is invalid")
        if not self.actor_id.strip():
            raise IdentityError("actor ID must not be empty")
        if self.source == "web":
            if self.email is None or self.actor_id != normalize_email(self.email):
                raise IdentityError("web actor ID must be its normalized email")
        elif self.email is not None:
            raise IdentityError("only web actors may have an email")

    @property
    def is_web(self) -> bool:
        return self.source == "web"

    def public_dict(self) -> dict[str, str | None]:
        return {"id": self.actor_id, "source": self.source, "email": self.email}


VOICE_ACTOR = Actor("household:voice", "voice")
SYSTEM_ACTOR = Actor("miso:system", "system")


def web_actor(email: str) -> Actor:
    normalized = normalize_email(email)
    return Actor(normalized, "web", normalized)


class HouseholdIdentityPolicy:
    """Resolve allowed members and enforce shared/private ownership semantics."""

    def __init__(self, allowed_emails: Iterable[str], local_dashboard_email: str) -> None:
        local = normalize_email(local_dashboard_email)
        allowed = {normalize_email(email) for email in allowed_emails}
        allowed.add(local)
        self.allowed_emails = frozenset(allowed)
        self.local_dashboard_email = local

    @property
    def local_actor(self) -> Actor:
        return web_actor(self.local_dashboard_email)

    def web_actor(self, email: str) -> Actor:
        actor = web_actor(email)
        if actor.actor_id not in self.allowed_emails:
            raise PermissionError("household member is not allowed")
        return actor


def private_owner(actor: Actor, visibility: str) -> str | None:
    if visibility not in {"shared", "private"}:
        raise IdentityError("visibility must be shared or private")
    if visibility == "shared":
        return None
    if not actor.is_web or actor.email is None:
        raise PermissionError("private records require an authenticated web user")
    return actor.email


def can_access(actor: Actor, visibility: str, owner_email: str | None) -> bool:
    return visibility == "shared" or (
        visibility == "private" and actor.is_web and actor.email == owner_email
    )
