import unittest

from miso.identity import (
    HouseholdIdentityPolicy,
    IdentityError,
    VOICE_ACTOR,
    can_access,
    private_owner,
    web_actor,
)


class HouseholdIdentityPolicyTests(unittest.TestCase):
    def test_normalizes_cloudflare_and_local_web_identities(self) -> None:
        policy = HouseholdIdentityPolicy("local@miso.invalid")
        self.assertEqual(policy.web_actor("juan@example.com").actor_id, "juan@example.com")
        self.assertEqual(policy.local_actor.actor_id, "local@miso.invalid")
        self.assertEqual(
            policy.web_actor("STRANGER@Example.com").actor_id,
            "stranger@example.com",
        )

    def test_shared_and_private_rules_distinguish_voice_from_web(self) -> None:
        juan = web_actor("juan@example.com")
        ana = web_actor("ana@example.com")
        self.assertTrue(can_access(VOICE_ACTOR, "shared", None))
        self.assertTrue(can_access(juan, "private", "juan@example.com"))
        self.assertFalse(can_access(ana, "private", "juan@example.com"))
        with self.assertRaises(PermissionError):
            private_owner(VOICE_ACTOR, "private")
        with self.assertRaises(IdentityError):
            web_actor("not-an-email")


if __name__ == "__main__":
    unittest.main()
