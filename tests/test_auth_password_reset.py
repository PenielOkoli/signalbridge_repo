from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from auth_manager import AuthManager, InvalidCredentialsError, PasswordResetError, SessionValidationError


class PasswordResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.users_path = Path(self.temp_dir.name) / "users.json"
        self.manager = AuthManager(self.users_path, signing_secret="test-signing-secret")
        self.user = self.manager.create_user("trader@example.com", "old-password", "Trader")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_reset_link_is_hashed_single_use_and_changes_the_password(self) -> None:
        token = self.manager.create_password_reset_token("trader@example.com")

        self.assertIsNotNone(token)
        self.assertNotIn(token or "", self.users_path.read_text(encoding="utf-8"))
        self.manager.reset_password(token or "", "new-password")

        with self.assertRaises(InvalidCredentialsError):
            self.manager.authenticate("trader@example.com", "old-password")
        self.assertEqual(self.manager.authenticate("trader@example.com", "new-password").id, self.user.id)
        with self.assertRaises(PasswordResetError):
            self.manager.reset_password(token or "", "another-password")

    def test_password_reset_revokes_existing_sessions(self) -> None:
        session = self.manager.issue_session(self.user)
        token = self.manager.create_password_reset_token("trader@example.com")
        self.manager.reset_password(token or "", "new-password")

        with self.assertRaises(SessionValidationError):
            self.manager.verify_session(session)

        fresh_session = self.manager.issue_session(self.manager.authenticate("trader@example.com", "new-password"))
        self.assertEqual(self.manager.verify_session(fresh_session).sub, self.user.id)

    def test_unknown_email_does_not_create_a_recovery_token(self) -> None:
        self.assertIsNone(self.manager.create_password_reset_token("unknown@example.com"))

