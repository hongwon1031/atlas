"""CLI 인자 해석과 config 테스트.

네트워크를 타는 경로는 여기서 실행하지 않고 인자 정규화와 설정 병합만 검증합니다.
"""

import contextlib
import io
import unittest

from atlas.cli import _config, _normalize, _option, build_parser
from atlas.config import ClaimConfig, PollingConfig, WorkerConfig


class LegacyInvocationTest(unittest.TestCase):
    """`python -m atlas <issue-number>`가 계속 동작해야 합니다."""

    def test_bare_issue_number_becomes_show(self):
        self.assertEqual(_normalize(["12"]), ["show", "12"])

    def test_bare_issue_number_after_global_flags(self):
        self.assertEqual(
            _normalize(["--repository", "o/n", "12"]),
            ["--repository", "o/n", "show", "12"],
        )

    def test_explicit_subcommands_are_untouched(self):
        for argv in (["show", "12"], ["poll"], ["claim"], ["tasks"], ["release", "claim-1"]):
            self.assertEqual(_normalize(list(argv)), argv)

    def test_flag_only_argv_is_untouched(self):
        self.assertEqual(_normalize(["--help"]), ["--help"])

    def test_legacy_form_parses_to_the_same_namespace_as_show(self):
        parser = build_parser()

        legacy = parser.parse_args(_normalize(["12"]))
        explicit = parser.parse_args(_normalize(["show", "12"]))

        self.assertEqual(legacy.command, "show")
        self.assertEqual(legacy.issue_number, 12)
        self.assertEqual(vars(legacy), vars(explicit))


class ParserTest(unittest.TestCase):
    def test_poll_accepts_watch_and_interval(self):
        args = build_parser().parse_args(["poll", "--watch", "--interval", "5", "--iterations", "2"])

        self.assertEqual(args.command, "poll")
        self.assertTrue(args.watch)
        self.assertEqual(args.interval, 5.0)
        self.assertEqual(args.iterations, 2)

    def test_claim_accepts_worker_and_lease(self):
        args = build_parser().parse_args(["claim", "--worker-id", "w1", "--lease-ttl", "60"])

        self.assertEqual(args.worker_id, "w1")
        self.assertEqual(args.lease_ttl, 60.0)

    def test_command_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args([])


class CommonOptionTest(unittest.TestCase):
    """subparser 기본값이 상위 parser가 읽은 값을 덮어쓰면 안 됩니다."""

    def test_database_before_subcommand_survives(self):
        args = build_parser().parse_args(["--database", "/tmp/a.db", "tasks"])

        self.assertEqual(_config(args).database_path, "/tmp/a.db")

    def test_database_after_subcommand_also_works(self):
        args = build_parser().parse_args(["tasks", "--database", "/tmp/b.db"])

        self.assertEqual(_config(args).database_path, "/tmp/b.db")

    def test_repository_before_subcommand_survives(self):
        args = build_parser().parse_args(["--repository", "o/n", "poll"])

        self.assertEqual(_config(args).polling.repository, "o/n")

    def test_indent_before_subcommand_survives(self):
        args = build_parser().parse_args(["--indent", "0", "tasks"])

        self.assertEqual(_option(args, "indent", 2), 0)

    def test_indent_defaults_when_absent(self):
        args = build_parser().parse_args(["tasks"])

        self.assertEqual(_option(args, "indent", 2), 2)

    def test_legacy_form_keeps_global_flags(self):
        args = build_parser().parse_args(_normalize(["--repository", "o/n", "12"]))

        self.assertEqual(args.command, "show")
        self.assertEqual(args.issue_number, 12)
        self.assertEqual(_option(args, "repository"), "o/n")

    def test_legacy_form_accepts_trailing_flags(self):
        args = build_parser().parse_args(_normalize(["12", "--indent", "0"]))

        self.assertEqual(args.issue_number, 12)
        self.assertEqual(_option(args, "indent", 2), 0)


class NegativeArgumentTest(unittest.TestCase):
    """음수 interval과 lease TTL은 argparse 단계에서 거부해야 합니다."""

    def parse(self, argv):
        with contextlib.redirect_stderr(io.StringIO()):
            return build_parser().parse_args(argv)

    def test_negative_interval_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.parse(["poll", "--interval", "-1"])

    def test_zero_interval_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.parse(["poll", "--interval", "0"])

    def test_negative_lease_ttl_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.parse(["claim", "--lease-ttl", "-1"])

    def test_negative_iterations_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.parse(["poll", "--watch", "--iterations", "-1"])

    def test_non_positive_issue_number_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.parse(_normalize(["0"]))

    def test_positive_values_are_accepted(self):
        args = self.parse(["poll", "--interval", "5", "--iterations", "2"])

        self.assertEqual(args.interval, 5.0)
        self.assertEqual(args.iterations, 2)


class ConfigTest(unittest.TestCase):
    def test_defaults_are_conservative(self):
        config = WorkerConfig()

        self.assertEqual(config.database_path, "atlas.db")
        self.assertEqual(config.polling.repository, "hongwon1031/atlas")
        # approval gate는 기본으로 켜져 있어야 합니다.
        self.assertTrue(config.polling.require_queue_label)
        self.assertGreater(config.claim.lease_ttl_seconds, 0)
        self.assertEqual(config.claim.grace_period_seconds, 0.0)

    def test_from_env_reads_overrides(self):
        config = WorkerConfig.from_env(
            {
                "ATLAS_DB_PATH": "/tmp/x.db",
                "ATLAS_REPOSITORY": "owner/name",
                "ATLAS_POLL_INTERVAL_SECONDS": "15",
                "ATLAS_LEASE_TTL_SECONDS": "120",
                "ATLAS_DISABLE_QUEUE_LABEL": "true",
            }
        )

        self.assertEqual(config.database_path, "/tmp/x.db")
        self.assertEqual(config.polling.repository, "owner/name")
        self.assertEqual(config.polling.interval_seconds, 15.0)
        self.assertEqual(config.claim.lease_ttl_seconds, 120.0)
        self.assertFalse(config.polling.require_queue_label)

    def test_from_env_ignores_invalid_numbers(self):
        config = WorkerConfig.from_env(
            {"ATLAS_POLL_INTERVAL_SECONDS": "abc", "ATLAS_LEASE_TTL_SECONDS": "-5"}
        )

        self.assertEqual(config.polling.interval_seconds, PollingConfig().interval_seconds)
        self.assertEqual(config.claim.lease_ttl_seconds, ClaimConfig().lease_ttl_seconds)

    def test_from_env_with_no_variables_matches_defaults(self):
        self.assertEqual(WorkerConfig.from_env({}), WorkerConfig())

    def test_negative_config_values_are_rejected(self):
        with self.assertRaises(ValueError):
            PollingConfig(interval_seconds=-1)
        with self.assertRaises(ValueError):
            PollingConfig(per_page=0)
        with self.assertRaises(ValueError):
            ClaimConfig(lease_ttl_seconds=-1)
        with self.assertRaises(ValueError):
            ClaimConfig(grace_period_seconds=-1)

    def test_zero_grace_period_is_allowed(self):
        self.assertEqual(ClaimConfig(grace_period_seconds=0).grace_period_seconds, 0)

    def test_backoff_is_capped(self):
        config = PollingConfig(
            backoff_initial_seconds=5.0, backoff_multiplier=3.0, backoff_max_seconds=20.0
        )

        self.assertEqual(config.backoff_delay(1), 5.0)
        self.assertEqual(config.backoff_delay(2), 15.0)
        self.assertEqual(config.backoff_delay(3), 20.0)
        self.assertEqual(config.backoff_delay(99), 20.0)


if __name__ == "__main__":
    unittest.main()
