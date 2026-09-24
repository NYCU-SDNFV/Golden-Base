import hashlib
import json
import unittest

from tools.release.tests import test_release as SHARED


RELEASE, RUNTIME, STRIP = SHARED.RELEASE, SHARED.RUNTIME, SHARED.STRIP
commit, metadata, put = SHARED.commit, SHARED.metadata, SHARED.put


class RuntimeDistributionTests(SHARED.TempCase):
    source_tree = SHARED.SanitizationAndBundleTests.source_tree

    def test_one_pinned_implementation_is_injected_into_both_outputs(self):
        source, cfg = self.source_tree()
        student, bundle = self.root / "student", self.root / "canonical"
        RELEASE.build_student(source, student, metadata(cfg))
        RELEASE.build_bundle(source, student, bundle)
        self.assertEqual("vrouter", RUNTIME.validate(source)["environment"])
        self.assertFalse((student / ".github/golden/grading.py").exists())
        self.assertFalse((student / ".github/grade").exists())
        expected = RUNTIME.expected_files()
        for name in RUNTIME.public_paths():
            raw = (source / name).read_bytes() if name == RUNTIME.PROFILE else expected[name]
            self.assertEqual(raw, (student / name).read_bytes(), name)
        for name in RUNTIME.LIBRARIES:
            self.assertEqual(
                expected[f".github/golden/{name}"],
                (bundle / "golden" / name).read_bytes(), name,
            )
        self.assertEqual(expected[RUNTIME.ADAPTER], (bundle / "autograder.py").read_bytes())
        provenance = json.loads((student / RUNTIME.VERSION).read_text(encoding="utf-8"))
        self.assertEqual(RUNTIME.toolkit_ref(), provenance["toolkit_ref"])
        for name, digest in provenance["files"].items():
            self.assertEqual(hashlib.sha256((source / name).read_bytes()).hexdigest(), digest)

    def test_edited_shared_adapter_is_rejected_without_executing_it(self):
        source, cfg = self.source_tree()
        put(source, RUNTIME.ADAPTER, "raise RuntimeError('not trusted')\n")
        output = self.root / "student"
        with self.assertRaisesRegex(RELEASE.ReleaseError, "outdated or edited shared runtime"):
            RELEASE.build_student(source, output, metadata(cfg))
        self.assertFalse(output.exists())

    def test_missing_bootstrap_cannot_silently_revert_to_a_lab_adapter(self):
        source, cfg = self.source_tree()
        (source / ".github/golden/environment.py").unlink()
        with self.assertRaisesRegex(RELEASE.ReleaseError, "regular file"):
            RELEASE.build_student(source, self.root / "student", metadata(cfg))

    def test_wrong_caller_pin_and_wrong_provenance_are_rejected(self):
        source, _ = self.source_tree()
        caller = source / ".github/workflows/release.yml"
        original = caller.read_bytes()
        put(source, caller.relative_to(source), original.replace(
            RUNTIME.toolkit_ref().encode(), b"0" * 40))
        with self.assertRaisesRegex(RUNTIME.RuntimeContractError, "same Golden-Base commit"):
            RUNTIME.validate(source)
        put(source, caller.relative_to(source), original)
        stamp = json.loads((source / RUNTIME.VERSION).read_text(encoding="utf-8"))
        stamp["toolkit_ref"] = "0" * 40
        put(source, RUNTIME.VERSION, json.dumps(stamp))
        with self.assertRaisesRegex(RUNTIME.RuntimeContractError, "outdated or edited"):
            RUNTIME.validate(source)

    def test_common_files_cannot_be_unprotected_or_extended_by_source(self):
        source, _ = self.source_tree()
        manifest = source / RELEASE.MANIFEST
        original = manifest.read_bytes()
        lines = original.decode().splitlines(keepends=True)
        put(source, RELEASE.MANIFEST, "".join(
            line for line in lines if not line.rstrip().endswith(RUNTIME.PROFILE)))
        with self.assertRaisesRegex(RUNTIME.RuntimeContractError, "complete runtime contract"):
            RUNTIME.validate(source)
        put(source, RELEASE.MANIFEST, original)
        put(source, ".github/golden/override.py", "raise RuntimeError('not a toolkit asset')\n")
        commit(source)
        with self.assertRaisesRegex(RUNTIME.RuntimeContractError, "unexpected files"):
            RUNTIME.validate(source)

    def test_profile_must_match_source_identity_and_real_kernel_requirements(self):
        source, _ = self.source_tree()
        original = json.loads((source / RUNTIME.PROFILE).read_text(encoding="utf-8"))
        for field, value, error in (
                ("name", "Toolchain", "match .release.json"),
                ("assignment", "lab0-toolchain", "configured release route"),
                ("environment", "toolchain", "requires the vrouter")):
            put(source, RUNTIME.PROFILE, json.dumps(dict(original, **{field: value})))
            with self.subTest(field=field), self.assertRaisesRegex(
                    RUNTIME.RuntimeContractError, error):
                RUNTIME.validate(source)

    def test_pretest_cannot_check_a_different_healthy_image(self):
        source, _ = self.source_tree()
        put(source, "Dockerfile", "FROM ghcr.io/nycu-sdnfv/lab-base:115-1\n")
        with self.assertRaisesRegex(RUNTIME.RuntimeContractError, "exact immutable"):
            RUNTIME.validate(source)

    def test_runtime_count_must_match_lab_rubric_without_changing_points(self):
        source, cfg = self.source_tree()
        profile = RUNTIME.CONTRACT.load_profile(source / RUNTIME.PROFILE)
        profile["checks"] += 1
        put(source, RUNTIME.PROFILE, json.dumps(profile))
        RUNTIME.synchronize(source)
        commit(source)
        student = self.root / "student"
        RELEASE.build_student(source, student, metadata(cfg))
        with self.assertRaisesRegex(RELEASE.ReleaseError, "rubric count"):
            RELEASE.build_bundle(source, student, self.root / "bundle")

    def test_generated_modes_are_explicit_and_unrecognized_bundle_paths_fail(self):
        source, cfg = self.source_tree()
        student, bundle = self.root / "student", self.root / "bundle"
        RELEASE.build_student(source, student, metadata(cfg))
        RELEASE.build_bundle(source, student, bundle)
        modes = RELEASE.source_file_modes(source)
        actual = RELEASE.bundle_file_modes(bundle, modes)
        for name in RUNTIME.canonical_modes():
            self.assertEqual("100644", actual[name], name)
        self.assertEqual("100755", actual["tests/00_env.sh"])
        put(bundle, "golden/not-in-the-toolkit.py", "pass\n")
        with self.assertRaisesRegex(RELEASE.ReleaseError, "not in the tagged source"):
            RELEASE.bundle_file_modes(bundle, modes)

    def test_public_runtime_is_injected_even_if_source_ignore_omits_it(self):
        source, cfg = self.source_tree()
        put(source, ".studentignore", "solution.txt\n.github/golden/\n")
        commit(source)
        student = self.root / "student"
        RELEASE.build_student(source, student, metadata(cfg))
        self.assertTrue((student / RUNTIME.PROFILE).is_file())
        self.assertFalse((student / ".github/golden/grading.py").exists())
        self.assertEqual([], STRIP.guards(student))


class ProfileValidationTests(unittest.TestCase):
    def test_all_declared_profiles_are_data_only_and_strict(self):
        valid = {"schema": 1, "name": "Demo", "lab": 9, "assignment": "lab9-demo",
                 "checks": 1, "environment": "toolchain"}
        for environment in RUNTIME.CONTRACT.ENVIRONMENTS:
            value = dict(valid, environment=environment)
            self.assertEqual(value, RUNTIME.CONTRACT.validate_profile(value))
        for field, value in (
                ("schema", True), ("lab", True), ("checks", 0), ("checks", True),
                ("environment", "automatic"), ("name", "../Demo"),
                ("assignment", "invalid/route")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                RUNTIME.CONTRACT.validate_profile(dict(valid, **{field: value}))
        with self.assertRaises(ValueError):
            RUNTIME.CONTRACT.validate_profile(dict(valid, run="arbitrary source command"))


if __name__ == "__main__":
    unittest.main()
