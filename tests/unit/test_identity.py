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
    def test_normalizes_allowed_members_and_rejects_unknown_email(self) -> None:
        policy = HouseholdIdentityPolicy(
            ("JUAN@Example.com", "ana@example.com"), "local@miso.invalid"
        )
        self.assertEqual(policy.web_actor("juan@example.com").actor_id, "juan@example.com")
        self.assertEqual(policy.local_actor.actor_id, "local@miso.invalid")
        with self.assertRaises(PermissionError):
            policy.web_actor("stranger@example.com")

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
