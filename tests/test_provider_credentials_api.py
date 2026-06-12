import unittest

from pydantic import ValidationError

from app.models.provider import Provider
from app.routers.provider_credentials import _serialize_provider_credential
from app.schemas.provider import ProviderCredentialUpdateRequest
from app.utils.timezone import now_beijing


class ProviderCredentialsApiRegressionTest(unittest.TestCase):
    def test_password_is_not_trimmed_but_username_and_api_key_are_normalized(self):
        payload = ProviderCredentialUpdateRequest(
            username=" admin ",
            password=" pass-with-space ",
            provider_id=1,
            api_key=" sk-new ",
        )

        self.assertEqual(payload.username, "admin")
        self.assertEqual(payload.password, " pass-with-space ")
        self.assertEqual(payload.api_key, "sk-new")

    def test_blank_api_key_is_rejected(self):
        with self.assertRaises(ValidationError):
            ProviderCredentialUpdateRequest(
                username="admin",
                password="password",
                provider_id=1,
                api_key="   ",
            )

    def test_provider_credential_serializer_returns_raw_and_masked_key(self):
        now = now_beijing()
        provider = Provider(
            id=23,
            name="测试提供商",
            base_url="https://example.test/v1",
            api_key="sk-abcdefghijklmnopqrstuvwxyz",
            enabled=True,
            priority=100,
            timeout_ms=30000,
            max_retries=2,
            updated_at=now,
        )

        item = _serialize_provider_credential(provider)

        self.assertEqual(item.id, 23)
        self.assertEqual(item.name, "测试提供商")
        self.assertEqual(item.api_key, "sk-abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(item.masked_api_key, "sk-a...wxyz")


if __name__ == "__main__":
    unittest.main()
