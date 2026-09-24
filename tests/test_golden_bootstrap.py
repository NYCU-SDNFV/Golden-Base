import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("golden_bootstrap", ROOT / "tools/golden-bootstrap.py")
BOOTSTRAP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BOOTSTRAP)


class API:
    def __init__(self):
        self.writes = []
        self.secret = {"visibility": "selected"}
        self.repo = {"id": 123, "full_name": "NYCU-SDNFV/Golden-Demo", "private": True,
                     "is_template": False, "archived": False, "fork": False,
                     "permissions": {"admin": True}, "size": 10, "default_branch": "master"}
        self.teams = [{"slug": "instructors"}]
        self.collaborators = [{"login": "teacher"}]
        self.student_marker = None
        self.member_role = "admin"
        self.selected = []

    def call(self, method, path, data=None, missing_ok=False):
        if method != "GET":
            self.writes.append((method, path, data))
            if method == "POST":
                self.repo = dict(self.repo or {}, id=123, full_name="NYCU-SDNFV/Golden-Demo",
                                 private=True, is_template=False, permissions={"admin": True}, size=0)
                return self.repo
            if "/actions/secrets/" in path and "/repositories/" in path:
                self.selected = [{"id": 123}]
            return None
        if "/contents/.classroom50.yaml" in path:
            return self.student_marker
        if "/actions/secrets/" in path:
            return {"repositories": self.selected} if "/repositories?" in path else self.secret
        if "/teams?" in path:
            return self.teams
        if "/collaborators?" in path:
            return self.collaborators
        if "/teams/instructors/memberships/" in path:
            return None
        if "/memberships/" in path:
            return {"state": "active", "role": self.member_role}
        if path == "/repos/NYCU-SDNFV/Golden-Demo":
            return self.repo
        raise AssertionError((method, path))


def configuration():
    return {"schema": 1, "name": "Demo", "title": "Lab Demo", "description": "Example",
            "owner": "NYCU-SDNFV", "default_channel": "newbie",
            "config_repository": "NYCU-SDNFV/classroom50", "config_branch": "main",
            "pages_url": "https://nycu-sdnfv.github.io/classroom50", "student_updates": "manual",
            "channels": {"newbie": {"classroom": "winlab-newbies", "slug": "lab9-demo",
                                    "template": "Golden-newbie-Demo"}}}


class EnrollmentTests(unittest.TestCase):
    def test_dry_run_has_no_mutations(self):
        api = API()
        self.assertFalse(BOOTSTRAP.enroll(api, "Demo")["applied"])
        self.assertEqual([], api.writes)

    def test_enrollment_adds_one_repository_id_without_replacing_org_visibility(self):
        api = API()
        result = BOOTSTRAP.enroll(api, "Demo", apply=True)
        self.assertTrue(result["applied"])
        self.assertEqual(2, len(api.writes))
        self.assertEqual("/orgs/NYCU-SDNFV/actions/secrets/GOLDEN_RELEASE_TOKEN/repositories/123",
                         api.writes[-1][1])
        self.assertIsNone(api.writes[-1][2])

    def test_all_private_or_missing_secret_is_rejected_before_mutation(self):
        for secret in (None, {"visibility": "all"}, {"visibility": "private"}):
            api = API()
            api.secret = secret
            with self.subTest(secret=secret), self.assertRaises(BOOTSTRAP.BootstrapError):
                BOOTSTRAP.enroll(api, "Demo", apply=True)
            self.assertEqual([], api.writes)

    def test_student_checkout_cannot_receive_publisher_secret(self):
        api = API()
        api.student_marker = {"name": ".classroom50.yaml"}
        with self.assertRaises(BOOTSTRAP.BootstrapError):
            BOOTSTRAP.enroll(api, "Demo", apply=True)
        self.assertEqual([], api.writes)

    def test_public_template_and_fork_are_rejected(self):
        for field, value in (("private", False), ("is_template", True), ("fork", True)):
            api = API()
            api.repo[field] = value
            with self.subTest(field=field), self.assertRaises(BOOTSTRAP.BootstrapError):
                BOOTSTRAP.enroll(api, "Demo", apply=True)
            self.assertEqual([], api.writes)

    def test_student_team_or_noninstructor_collaborator_is_rejected(self):
        for group in ("team", "collaborator"):
            api = API()
            if group == "team":
                api.teams.append({"slug": "classroom50-students"})
            else:
                api.member_role = "member"
            with self.subTest(group=group), self.assertRaises(BOOTSTRAP.BootstrapError):
                BOOTSTRAP.enroll(api, "Demo", apply=True)
            self.assertEqual([], api.writes)

    def test_snapshot_name_is_not_an_instructor_source_name(self):
        for name in ("Base", "newbie-Demo", "../Demo", "115-1-Demo"):
            with self.subTest(name=name), self.assertRaises(BOOTSTRAP.BootstrapError):
                BOOTSTRAP.enroll(API(), name, apply=True)


class WorkflowTests(unittest.TestCase):
    def test_new_runtime_profile_requires_explicit_lab_capabilities(self):
        value = BOOTSTRAP.new_runtime_profile("Demo", 9, "lab9-demo", 4, "toolchain")
        self.assertEqual(4, value["checks"])
        self.assertEqual("toolchain", value["environment"])
        for checks, environment in ((None, "toolchain"), (4, None), (True, "toolchain")):
            with self.subTest(checks=checks, environment=environment), self.assertRaises(
                    BOOTSTRAP.BootstrapError):
                BOOTSTRAP.new_runtime_profile("Demo", 9, "lab9-demo", checks, environment)

    def test_new_lab_configuration_needs_no_hand_written_release_routes(self):
        config = BOOTSTRAP.new_configuration("Demo", "lab9-demo", "Lab 9", "A new lab", "115-1")
        self.assertEqual("Golden-newbie-Demo", config["channels"]["newbie"]["template"])
        self.assertEqual("sdnfv-115-1", config["channels"]["115-1"]["classroom"])
        self.assertEqual("manual", config["student_updates"])

    def test_same_immutable_ref_and_explicit_secret_are_generated(self):
        sha = "a" * 40
        workflow = BOOTSTRAP.render_workflow(sha, "poc.yml", "token")
        self.assertIn("shared-release.yml@" + sha, workflow)
        self.assertIn("toolkit_ref: " + sha, workflow)
        self.assertIn("needs: verify", workflow)
        self.assertIn("secrets.GOLDEN_RELEASE_TOKEN", workflow)
        self.assertNotIn("secrets: inherit", workflow)

    def test_app_auth_uses_the_shared_org_app_identity(self):
        workflow = BOOTSTRAP.render_workflow("b" * 40, "poc.yml", "app")
        self.assertIn("vars.GOLDEN_RELEASE_APP_ID", workflow)
        self.assertIn("secrets.GOLDEN_RELEASE_APP_KEY", workflow)
        self.assertNotIn("secrets.GOLDEN_RELEASE_TOKEN", workflow)

    def test_floating_toolkit_and_unsafe_workflow_path_are_rejected(self):
        for ref, path in (("main", "poc.yml"), ("a" * 40, "../poc.yml"), ("a" * 40, "release.yml")):
            with self.subTest(ref=ref, path=path), self.assertRaises(BOOTSTRAP.BootstrapError):
                BOOTSTRAP.render_workflow(ref, path, "token")

    def test_configuration_migration_is_explicit_and_idempotent(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / ".github/workflows").mkdir(parents=True)
            (root / ".github/workflows/poc.yml").write_text("name: private verification\n")
            (root / ".github/workflows/release.yml").write_text("old workflow\n")
            (root / ".release.json").write_text(json.dumps(configuration()))
            profile = root / ".github/golden/profile.json"
            profile.parent.mkdir()
            profile.write_text(json.dumps(
                BOOTSTRAP.new_runtime_profile("Demo", 9, "lab9-demo", 4, "toolchain")))
            api = API()
            with self.assertRaises(BOOTSTRAP.BootstrapError):
                BOOTSTRAP.configure(api, root, "Demo", "a" * 40, "poc.yml", "token", apply=True)
            self.assertEqual([], api.writes)
            BOOTSTRAP.configure(api, root, "Demo", "a" * 40, "poc.yml", "token",
                                apply=True, replace_workflow=True)
            before = (root / ".github/workflows/release.yml").read_bytes()
            BOOTSTRAP.configure(api, root, "Demo", "a" * 40, "poc.yml", "token", apply=True)
            self.assertEqual(before, (root / ".github/workflows/release.yml").read_bytes())

    def test_missing_or_mismatched_profile_stops_before_secret_enrollment(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / ".github/workflows").mkdir(parents=True)
            (root / ".github/workflows/poc.yml").write_text("name: private verification\n")
            (root / ".release.json").write_text(json.dumps(configuration()))
            api = API()
            with self.assertRaisesRegex(BOOTSTRAP.BootstrapError, "declare"):
                BOOTSTRAP.configure(api, root, "Demo", "a" * 40, "poc.yml", "token", apply=True)
            wrong = BOOTSTRAP.new_runtime_profile("Different", 9, "lab9-demo", 4, "toolchain")
            with self.assertRaisesRegex(BOOTSTRAP.BootstrapError, "identify this source"):
                BOOTSTRAP.configure(api, root, "Demo", "a" * 40, "poc.yml", "token",
                                    apply=True, new_profile=wrong)
            self.assertEqual([], api.writes)

    def test_bootstrap_generates_profile_without_running_source_code(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / ".github/workflows").mkdir(parents=True)
            (root / ".github/workflows/poc.yml").write_text("name: private verification\n")
            profile = BOOTSTRAP.new_runtime_profile("Demo", 9, "lab9-demo", 4, "toolchain")
            result = BOOTSTRAP.configure(
                API(), root, "Demo", "a" * 40, "poc.yml", "token",
                new_config=configuration(), new_profile=profile)
            self.assertTrue(result["runtime_sync_required"])
            self.assertFalse((root / ".github/golden/profile.json").exists())
            BOOTSTRAP.configure(
                API(), root, "Demo", "a" * 40, "poc.yml", "token", apply=True,
                new_config=configuration(), new_profile=profile)
            self.assertEqual(profile, json.loads(
                (root / ".github/golden/profile.json").read_text(encoding="utf-8")))

    def test_renumbered_newbie_route_remains_a_newbie_channel(self):
        value = configuration()
        value["channels"]["newbie-115-1"] = {
            "classroom": "winlab-newbies", "slug": "lab8-demo",
            "template": "Golden-newbie-115-1-Demo",
        }
        self.assertEqual(value, BOOTSTRAP.validate_config(value, "Demo"))
        self.assertIn('"newbie-*-v*"', BOOTSTRAP.render_workflow("a" * 40, "poc.yml", "token"))

    def test_wrong_assignment_namespace_is_rejected(self):
        config = configuration()
        config["channels"]["newbie"]["classroom"] = "someone-else"
        with self.assertRaises(BOOTSTRAP.BootstrapError):
            BOOTSTRAP.validate_config(config, "Demo")


if __name__ == "__main__":
    unittest.main()
