# SPDX-License-Identifier: MIT

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci import APK_NAMES, CHECKSUMS, check_tag, checksums, junit, run_device, stage_host, verify_avd


class CiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_junit_rejects_missing_empty_failed_skipped_and_duplicate_results(self):
        with self.assertRaises(ValueError):
            junit(self.root)
        for body in ('', '<testcase name="one"><failure/></testcase>',
                     '<testcase name="one"><skipped/></testcase>',
                     '<testcase name="one"/><testcase name="one"/>'):
            (self.root / "TEST-suite.xml").write_text(f'<testsuite>{body}</testsuite>')
            with self.assertRaises(ValueError):
                junit(self.root)
        (self.root / "TEST-suite.xml").write_text('<testsuites><testsuite tests="1"><testcase classname="suite" name="one"/></testsuite></testsuites>')
        self.assertEqual(junit(self.root), {"status": "passed", "cases": 1})

    def stage(self, version_name="1.0"):
        build = self.root / "app/build"
        for suite in ("testDebugUnitTest", "validateDebugScreenshotTest"):
            directory = build / f"test-results/{suite}"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "TEST-suite.xml").write_text('<testsuite><testcase classname="suite" name="one"/></testsuite>')
        reports = build / "reports"
        reports.mkdir(exist_ok=True)
        lint = reports / "lint-results-debug.xml"
        lint.write_text('<issues><issue severity="Warning"/></issues>')
        coverage = reports / "jacoco/jacocoDebugReport/jacocoDebugReport.xml"
        coverage.parent.mkdir(parents=True, exist_ok=True)
        coverage.write_text('<report/>')
        for variant in ("debug", "release"):
            directory = build / f"outputs/apk/{variant}"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "app.apk").write_bytes(b"apk")
            (directory / "output-metadata.json").write_text(json.dumps({"applicationId": "test.app", "elements": [
                {"outputFile": "app.apk", "versionName": version_name, "versionCode": 1}]}))
        return lint, coverage

    def test_stage_host_checks_lint_coverage_reports_and_both_apk_variants(self):
        lint, coverage = self.stage()
        output = self.root / "staged"
        with patch("ci.toolchain", return_value={"java": "25"}):
            stage_host(self.root, output)
            self.assertTrue(all((output / name).is_file() for name in APK_NAMES.values()))
            lint.write_text('<issues><issue severity="Error"/></issues>')
            with self.assertRaisesRegex(ValueError, "lint"):
                stage_host(self.root, output)
            lint.write_text('<issues/>')
            coverage.unlink()
            with self.assertRaisesRegex(ValueError, "coverage"):
                stage_host(self.root, output)

    def test_stage_host_rejects_apk_filename_that_escapes_build_directory(self):
        self.stage()
        metadata = self.root / "app/build/outputs/apk/debug/output-metadata.json"
        metadata.write_text(json.dumps({"applicationId": "test.app", "elements": [
            {"outputFile": "../release/app.apk", "versionName": "1.0", "versionCode": 1}]}))
        with patch("ci.toolchain", return_value={}), self.assertRaisesRegex(ValueError, "Invalid APK"):
            stage_host(self.root, self.root / "staged")

    def test_checksums_cover_both_apks_and_reject_missing_or_empty(self):
        with self.assertRaisesRegex(ValueError, "Missing or empty"):
            checksums(self.root)
        for name in APK_NAMES.values():
            (self.root / name).write_bytes(b"apk " + name.encode())
        checksums(self.root)
        listed = (self.root / CHECKSUMS).read_text().splitlines()
        self.assertEqual(sorted(line.split("  ")[1] for line in listed), sorted(APK_NAMES.values()))
        for name in APK_NAMES.values():
            self.assertIn(f"{hashlib.sha256((self.root / name).read_bytes()).hexdigest()}  {name}", listed)
        (self.root / APK_NAMES["debug"]).write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "Missing or empty"):
            checksums(self.root)

    def test_release_tag_must_match_version_name(self):
        variants = {key: {"version_name": "1.2"} for key in ("debug", "release")}
        (self.root / "host-validation.json").write_text(json.dumps({"variants": variants}))
        check_tag("v1.2", self.root)
        for tag in ("1.2", "v1.3", "v1.2.0"):
            with self.assertRaisesRegex(ValueError, "does not match"):
                check_tag(tag, self.root)
        variants["release"]["version_name"] = "1.3"
        (self.root / "host-validation.json").write_text(json.dumps({"variants": variants}))
        with self.assertRaises(ValueError):
            check_tag("v1.2", self.root)

    def test_device_failure_still_collects_logs(self):
        def command(args, **kwargs):
            if args[0] == "bash":
                raise subprocess.CalledProcessError(1, args)
        with patch("ci.subprocess.run", side_effect=command) as run, self.assertRaises(subprocess.CalledProcessError):
            run_device(self.root, self.root / "logs")
        self.assertEqual(run.call_args.args[0], ["adb", "logcat", "-d"])
        self.assertTrue((self.root / "logs/android-logcat.log").exists())

    def test_zero_exit_without_executed_instrumentation_is_rejected_and_logged(self):
        with patch("ci.subprocess.run") as run, self.assertRaisesRegex(ValueError, "Missing or empty"):
            run_device(self.root, self.root / "logs")
        self.assertEqual(run.call_args.args[0], ["adb", "logcat", "-d"])
        self.assertTrue((self.root / "logs/android-device.log").exists())

    def test_api_37_avd_requires_correct_target_and_image_before_boot(self):
        config = self.root / "meshlight-api37.avd/config.ini"
        config.parent.mkdir()
        config.write_text("image.sysdir.1=system-images/android-37.0/google_apis/x86_64/\n")
        identity = self.root / "meshlight-api37.ini"
        output = self.root / "avd.json"
        with patch.dict(os.environ, {"ANDROID_HOME": str(self.root / "sdk")}):
            identity.write_text("target=android-0\n")
            with self.assertRaisesRegex(ValueError, "Incorrect Android AVD"):
                verify_avd(self.root, "meshlight-api37", output)
            self.assertEqual(json.loads(output.read_text())["target"], "android-0")
            identity.write_text("target=android-37.0\n")
            self.assertEqual(verify_avd(self.root, "meshlight-api37", output)["target"], "android-37.0")
            config.write_text("image.sysdir.1=system-images/android-36/google_apis/x86_64/\n")
            with self.assertRaisesRegex(ValueError, "Incorrect Android AVD"):
                verify_avd(self.root, "meshlight-api37", output)


if __name__ == "__main__":
    unittest.main()
