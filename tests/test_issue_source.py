import unittest

from atlas.issue_source import GitHubRestIssueSource, IssueSourceError, resolve_token


class TokenResolutionTest(unittest.TestCase):
    def test_prefers_atlas_specific_variable(self):
        token = resolve_token({"ATLAS_GITHUB_TOKEN": "a", "GITHUB_TOKEN": "b"})
        self.assertEqual(token, "a")

    def test_falls_back_to_github_token(self):
        self.assertEqual(resolve_token({"GITHUB_TOKEN": "b"}), "b")

    def test_blank_and_missing_values_resolve_to_none(self):
        self.assertIsNone(resolve_token({"GITHUB_TOKEN": "   "}))
        self.assertIsNone(resolve_token({}))


class ErrorClassificationTest(unittest.TestCase):
    def classify(self, status, headers=None):
        return GitHubRestIssueSource._classify(status, headers or {})

    def test_maps_status_codes_to_categories(self):
        self.assertEqual(self.classify(401).category, "authentication")
        self.assertEqual(self.classify(403).category, "authentication")
        self.assertEqual(self.classify(404).category, "not_found")
        self.assertEqual(self.classify(500).category, "provider_unavailable")
        self.assertEqual(self.classify(418).category, "provider_error")

    def test_exhausted_rate_limit_is_distinguished_from_auth_failure(self):
        error = self.classify(403, {"X-RateLimit-Remaining": "0"})
        self.assertEqual(error.category, "rate_limited")

    def test_429_is_rate_limited_even_without_ratelimit_header(self):
        """secondary rate limit 응답은 X-RateLimit-Remaining을 포함하지 않습니다."""

        self.assertEqual(self.classify(429).category, "rate_limited")

    def test_retry_after_seconds_are_captured(self):
        error = self.classify(429, {"Retry-After": "60"})

        self.assertEqual(error.category, "rate_limited")
        self.assertEqual(error.retry_after, 60)

    def test_http_date_retry_after_is_ignored_not_crashed(self):
        error = self.classify(429, {"Retry-After": "Wed, 01 Sep 2026 00:00:00 GMT"})

        self.assertEqual(error.category, "rate_limited")
        self.assertIsNone(error.retry_after)

    def test_messages_never_leak_credentials(self):
        for status in (401, 403, 404, 500, 418):
            message = self.classify(status).message
            self.assertNotIn("Bearer", message)
            self.assertNotIn("Authorization", message)


class RequestGuardTest(unittest.TestCase):
    def test_rejects_non_positive_issue_number_without_network_call(self):
        source = GitHubRestIssueSource(token="unused", api_base="http://127.0.0.1:1")

        with self.assertRaises(IssueSourceError) as caught:
            source.fetch_issue("hongwon1031/atlas", 0)

        self.assertEqual(caught.exception.category, "invalid_request")

    def test_authorization_header_is_set_only_when_token_exists(self):
        with_token = GitHubRestIssueSource(token="secret-value")._headers()
        without_token = GitHubRestIssueSource(token="")._headers()

        self.assertEqual(with_token["Authorization"], "Bearer secret-value")
        self.assertNotIn("Authorization", without_token)


if __name__ == "__main__":
    unittest.main()
