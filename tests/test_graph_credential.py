import os
import unittest
from unittest.mock import Mock, patch

from azure.identity import AzureCliCredential, ClientAssertionCredential

from src import sharepoint
from src.sharepoint import _github_oidc_assertion, default_graph_credential


GITHUB_OIDC_ENV = {
    "AZURE_CLIENT_ID": "client-1",
    "AZURE_TENANT_ID": "tenant-1",
    "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.example/request?api-version=2.0",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
}


class DefaultGraphCredentialTests(unittest.TestCase):
    def test_github_actions_uses_refreshing_client_assertion(self):
        with patch.dict(os.environ, GITHUB_OIDC_ENV, clear=True):
            credential = default_graph_credential()

        self.assertIsInstance(credential, ClientAssertionCredential)

    def test_falls_back_to_azure_cli_outside_github_actions(self):
        env = {"AZURE_CLIENT_ID": "client-1", "AZURE_TENANT_ID": "tenant-1"}
        with patch.dict(os.environ, env, clear=True):
            credential = default_graph_credential()

        self.assertIsInstance(credential, AzureCliCredential)

    def test_falls_back_to_azure_cli_without_client_configuration(self):
        env = {
            key: value
            for key, value in GITHUB_OIDC_ENV.items()
            if key.startswith("ACTIONS_")
        }
        with patch.dict(os.environ, env, clear=True):
            credential = default_graph_credential()

        self.assertIsInstance(credential, AzureCliCredential)


class GitHubOidcAssertionTests(unittest.TestCase):
    def test_requests_a_new_token_with_entra_audience_on_every_call(self):
        response = Mock()
        response.json.return_value = {"value": "jwt-assertion"}
        with (
            patch.dict(os.environ, GITHUB_OIDC_ENV, clear=True),
            patch.object(sharepoint.requests, "get", return_value=response) as get,
        ):
            first = _github_oidc_assertion()
            second = _github_oidc_assertion()

        self.assertEqual((first, second), ("jwt-assertion", "jwt-assertion"))
        self.assertEqual(get.call_count, 2)
        url = get.call_args.args[0]
        self.assertTrue(url.startswith("https://token.example/request?api-version=2.0&audience="))
        self.assertIn("api%3A%2F%2FAzureADTokenExchange", url)
        self.assertEqual(
            get.call_args.kwargs["headers"],
            {"Authorization": "Bearer request-token"},
        )
        response.raise_for_status.assert_called()

    def test_missing_request_environment_fails_closed(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(RuntimeError, "OIDC token request environment"),
        ):
            _github_oidc_assertion()

    def test_empty_token_response_fails_closed(self):
        response = Mock()
        response.json.return_value = {"value": ""}
        with (
            patch.dict(os.environ, GITHUB_OIDC_ENV, clear=True),
            patch.object(sharepoint.requests, "get", return_value=response),
            self.assertRaisesRegex(RuntimeError, "has no value"),
        ):
            _github_oidc_assertion()


if __name__ == "__main__":
    unittest.main()
