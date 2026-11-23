# SPDX-License-Identifier: MIT

"""CI helpers: report validation, APK staging, emulator checks, and release checks."""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APK_NAMES = {"debug": "meshlight-android-debug.apk", "release": "meshlight-android-release-unsigned.apk"}
CHECKSUMS = "SHA256SUMS"
# Must match the SDK packages installed by the workflows and the emulator image.
SDK = "android-37.0"
BUILD_TOOLS = "36.0.0"


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def junit(directory):
    """Fail on missing, empty, failed, skipped, or duplicated JUnit results."""
    reports = sorted(Path(directory).rglob("TEST-*.xml"))
    cases = []
    for report in reports:
        root = ET.parse(report).getroot()
        for suite in root.iter("testsuite"):
            if any(int(suite.get(key, "0")) for key in ("failures", "errors", "skipped", "disabled")):
                raise ValueError(f"Failed or skipped Android tests: {report}")
        for case in root.iter("testcase"):
            if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
                raise ValueError(f"Failed or skipped Android test: {report}: {case.get('name')}")
            cases.append((case.get("classname"), case.get("name")))
    if not cases:
        raise ValueError(f"Missing or empty Android JUnit reports: {directory}")
    if len(set(cases)) != len(cases):
        raise ValueError(f"Duplicate Android test results: {directory}")
    return {"status": "passed", "cases": len(cases)}


def toolchain(app):
    versions = (app / "gradle/libs.versions.toml").read_text(encoding="utf-8")
    wrapper = (app / "gradle/wrapper/gradle-wrapper.properties").read_text(encoding="utf-8")
    runtime = subprocess.run([str(Path(os.environ["JAVA_HOME"]) / "bin/java"), "-version"],
                             capture_output=True, text=True, check=True)
    return {"os": platform.platform(), "java": (runtime.stderr + runtime.stdout).strip(),
            "gradle": re.search(r"gradle-([\d.]+)-bin", wrapper).group(1),
            "agp": re.search(r'^agp\s*=\s*"([^"]+)"', versions, re.MULTILINE).group(1),
            "sdk": SDK, "build_tools": BUILD_TOOLS}


def stage_host(app, output):
    """Validate host reports, then copy the debug and release APKs under stable names."""
    app, output = Path(app), Path(output)
    tests = {"unit": junit(app / "app/build/test-results/testDebugUnitTest"),
             "screenshots": junit(app / "app/build/test-results/validateDebugScreenshotTest")}
    lint = ET.parse(app / "app/build/reports/lint-results-debug.xml").getroot()
    if any(issue.get("severity") in {"Error", "Fatal"} for issue in lint.iter("issue")):
        raise ValueError("Android lint reported errors")
    coverage = app / "app/build/reports/jacoco/jacocoDebugReport/jacocoDebugReport.xml"
    if not coverage.is_file() or not coverage.stat().st_size:
        raise ValueError("Missing Android coverage report")
    variants = {}
    output.mkdir(parents=True, exist_ok=True)
    for variant, name in APK_NAMES.items():
        directory = app / f"app/build/outputs/apk/{variant}"
        details = json.loads((directory / "output-metadata.json").read_text(encoding="utf-8"))
        elements = details["elements"]
        if len(elements) != 1:
            raise ValueError("Expected one Android APK per variant")
        entry = elements[0]
        # Do not allow output metadata to select files outside the build directory.
        filename = entry["outputFile"]
        if Path(filename).name != filename or "/" in filename or "\\" in filename:
            raise ValueError("Invalid APK output filename")
        source = directory / filename
        if not source.is_file() or not source.stat().st_size:
            raise ValueError(f"Missing or empty APK: {source}")
        shutil.copyfile(source, output / name)
        variants[variant] = {"application_id": details["applicationId"],
                             "version_name": entry["versionName"], "version_code": entry["versionCode"]}
    write_json(output / "host-validation.json",
               {"tests": tests, "variants": variants, "toolchain": toolchain(app)})


def stage_device(app, output):
    app = Path(app)
    write_json(output, {"tests": {"instrumentation": junit(app / "app/build/outputs/androidTest-results/connected/debug")},
                        "toolchain": dict(toolchain(app), emulator={"api": "37.0", "target": "google_apis", "arch": "x86_64"})})


def verify_avd(directory, name, output):
    """Reject old avdmanager's silent android-0 target before booting the image."""
    directory = Path(directory)

    def properties(path):
        return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
                    if "=" in line and not line.lstrip().startswith("#"))
    identity = properties(directory / f"{name}.ini")
    config = properties(directory / f"{name}.avd/config.ini")
    details = {"target": identity.get("target"), "image": config.get("image.sysdir.1"),
               "command_line_tools": str(Path(os.environ["ANDROID_HOME"]) / "cmdline-tools/latest")}
    write_json(output, details)
    if details["target"] != SDK or (
            details["image"] or "").replace("\\", "/").rstrip("/") != f"system-images/{SDK}/google_apis/x86_64":
        raise ValueError(f"Incorrect Android AVD metadata; check command-line tools selection: {details}")
    return details


def run_device(app, output):
    app, output = Path(app).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(["adb", "logcat", "-c"], check=True)
    try:
        with (output / "android-device.log").open("w", encoding="utf-8") as log:
            subprocess.run(["bash", "./gradlew", ":app:connectedDebugAndroidTest",
                            "--no-configuration-cache", "--console=plain"], cwd=app,
                           stdout=log, stderr=subprocess.STDOUT, check=True)
        # Some AGP device setup failures still return exit code zero. Validate
        # actual execution while the emulator is alive so diagnostics survive.
        junit(app / "app/build/outputs/androidTest-results/connected/debug")
    finally:
        with (output / "android-logcat.log").open("w", encoding="utf-8") as log:
            subprocess.run(["adb", "logcat", "-d"], stdout=log, stderr=subprocess.STDOUT, check=False)
        print((output / "android-device.log").read_text(encoding="utf-8", errors="replace"))


def checksums(directory):
    """Verify both APKs exist and write SHA256SUMS covering them."""
    directory = Path(directory)
    lines = []
    for name in sorted(APK_NAMES.values()):
        path = directory / name
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f"Missing or empty APK: {path}")
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {name}\n")
    (directory / CHECKSUMS).write_text("".join(lines), encoding="utf-8")


def check_tag(tag, directory):
    """A release tag must be `v<versionName>` of the staged APKs."""
    info = json.loads((Path(directory) / "host-validation.json").read_text(encoding="utf-8"))
    names = {variant["version_name"] for variant in info["variants"].values()}
    if len(names) != 1 or tag != f"v{next(iter(names))}":
        raise ValueError(f"Tag {tag!r} does not match app versionName(s) {sorted(names)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("stage-host", "stage-device", "run-device"):
        command = commands.add_parser(name)
        command.add_argument("--app", type=Path, default=ROOT)
        command.add_argument("--output", type=Path, required=True)
    avd = commands.add_parser("verify-avd")
    avd.add_argument("--name", required=True)
    avd.add_argument("--output", type=Path, required=True)
    for name in ("checksums", "check-tag"):
        command = commands.add_parser(name)
        command.add_argument("--directory", type=Path, required=True)
        if name == "check-tag":
            command.add_argument("--tag", required=True)
    args = parser.parse_args()
    try:
        if args.command == "stage-host":
            stage_host(args.app, args.output)
        elif args.command == "stage-device":
            stage_device(args.app, args.output)
        elif args.command == "run-device":
            run_device(args.app, args.output)
        elif args.command == "verify-avd":
            verify_avd(os.environ["ANDROID_AVD_HOME"], args.name, args.output)
        elif args.command == "checksums":
            checksums(args.directory)
        elif args.command == "check-tag":
            check_tag(args.tag, args.directory)
    except (ValueError, KeyError, TypeError, OSError, ET.ParseError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Android CI error: {error}\n")


if __name__ == "__main__":
    main()
