import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import mountctl as m


def identity(target, token, mount_id="10"):
    return dict(zip(m.IDENTITY_KEYS, [mount_id, "1", "0:42", "/", str(target), "fuse." + token, "bindfs"] ))


def native_identity(target, source, mount_id="10"):
    return dict(zip(m.IDENTITY_KEYS, [mount_id, "1", "0:42", str(source), str(target), "ext4", "/dev/test"]))


class FakeBackend:
    def __init__(self):
        self.actual = {}
        self.calls = []
        self.busy = set()
        self.fail_mount = False
        self.sources = {}

    def mounts(self):
        return copy.deepcopy(self.actual)

    def preflight(self, actions):
        pass

    def mount(self, source, target):
        self.calls.append(("mount", source, target))
        if self.fail_mount:
            raise m.MountError("mount failed")
        self.actual[target] = [native_identity(target, source)]
        self.sources[target] = source

    def samefile(self, source, target):
        return self.sources.get(target) == source

    def unmount(self, target):
        self.calls.append(("unmount", target))
        if target in self.busy:
            raise m.MountError("Device or resource busy")
        del self.actual[target]
        self.sources.pop(target, None)


class MemoryStore:
    def __init__(self, config):
        self.config = config
        self.saved = {}
        self.history = []

    def save(self, records):
        self.saved = copy.deepcopy(records)
        self.history.append(copy.deepcopy(records))


class MountctlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.source = self.base / "sdk"
        self.source.mkdir()
        self.other = self.base / "sdk2"
        self.other.mkdir()
        self.target = self.project / "sdk"
        self.config = self.project / "mounts.json"
        self.backend = FakeBackend()
        self.store = MemoryStore(self.config)

    def write_config(self, entries=None):
        if entries is None:
            entries = [{"source": "../sdk", "target": "sdk"}]
        self.config.write_text(json.dumps({"version": 1, "mounts": entries}))

    def desired(self):
        return {str(self.target): str(self.source)}

    def mount_initial(self):
        records = {}
        self.execute(self.desired(), records)
        return records

    def mount_legacy(self):
        self.target.mkdir(exist_ok=True)
        token = m.TOKEN_PREFIX + "a" * 32
        actual = identity(self.target, token)
        self.backend.actual[str(self.target)] = [actual]
        return {str(self.target): {
            "source": str(self.source), "target": str(self.target),
            "token": token, "mount": copy.deepcopy(actual),
        }}

    def execute(self, desired, records):
        plan = m.make_plan(desired, records, self.backend.mounts())
        return m.execute_plan(plan, records, self.store, self.backend, emit=lambda _: None)

    def test_relative_paths_are_based_on_config_not_cwd(self):
        self.write_config()
        self.assertEqual(m.load_manifest(self.config), self.desired())

    def test_environment_paths_and_missing_variables(self):
        self.write_config([{"source": "${MOUNTCTL_TEST_SDK}", "target": "sdk"}])
        with patch.dict("os.environ", {"MOUNTCTL_TEST_SDK": str(self.source)}):
            self.assertEqual(m.load_manifest(self.config), self.desired())
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(m.MountError):
                m.load_manifest(self.config)

    def test_bad_configuration_never_means_empty(self):
        for text in ["", "{", "{}", '{"version":1,"mounts":null}',
                     '{"version":1,"mounts":[],"extra":true}',
                     '{"version":1,"mounts":[],"mounts":[]}']:
            with self.subTest(text=text):
                self.config.write_text(text)
                with self.assertRaises(m.MountError):
                    m.load_manifest(self.config)

    def test_missing_source_rejected_before_any_changes(self):
        self.write_config([{"source": "../missing", "target": "sdk"}])
        with self.assertRaises(m.MountError):
            m.load_manifest(self.config)
        self.assertFalse(self.backend.calls)

    def test_targets_cannot_escape_project_or_use_symlinks(self):
        (self.project / "link").symlink_to(self.other, target_is_directory=True)
        for target in ["..", "../escape", ".", "link/child"]:
            with self.subTest(target=target):
                self.write_config([{"source": "../sdk", "target": target}])
                with self.assertRaises(m.MountError):
                    m.load_manifest(self.config)

    def test_duplicate_nested_and_recursive_mappings_rejected(self):
        for entries in [
            [{"source": "../sdk", "target": "sdk"}] * 2,
            [{"source": "../sdk", "target": "a"}, {"source": "../sdk2", "target": "a/b"}],
            [{"source": "..", "target": "sdk"}],
        ]:
            with self.subTest(entries=entries):
                self.write_config(entries)
                with self.assertRaises(m.MountError):
                    m.load_manifest(self.config)

    def test_mountinfo_decodes_paths_and_keeps_stacked_mounts(self):
        text = (
            "10 1 0:42 / /tmp/a\\040b rw - fuse.mountctl token rw\n"
            "11 1 0:43 / /tmp/a\\040b rw - fuse other\\134name rw\n"
        )
        result = m.parse_mountinfo(text)
        self.assertEqual(len(result["/tmp/a b"]), 2)
        self.assertEqual(result["/tmp/a b"][1]["source"], "other\\name")

    def test_mount_journals_intent_and_repeated_sync_does_nothing(self):
        records = self.mount_initial()
        self.assertIsNone(self.store.history[0][str(self.target)]["mount"])
        self.assertIsNotNone(records[str(self.target)]["mount"])
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.execute(self.desired(), records), 0)
        self.assertEqual(len(self.backend.calls), 1)

    def test_removing_entry_unmounts_without_deleting_directory_or_source(self):
        records = self.mount_initial()
        (self.source / "precious.txt").write_text("keep me")
        self.assertEqual(self.execute({}, records), 0)
        self.assertFalse(records)
        self.assertTrue(self.target.is_dir())
        self.assertEqual((self.source / "precious.txt").read_text(), "keep me")

    def test_source_change_unmounts_then_mounts(self):
        records = self.mount_initial()
        desired = {str(self.target): str(self.other)}
        self.assertEqual(self.execute(desired, records), 0)
        self.assertEqual(self.backend.calls[-2:], [
            ("unmount", str(self.target)), ("mount", str(self.other), str(self.target)),
        ])

    def test_busy_mount_keeps_record_and_prevents_replacement(self):
        records = self.mount_initial()
        before = copy.deepcopy(records)
        self.backend.busy.add(str(self.target))
        self.assertGreater(self.execute({str(self.target): str(self.other)}, records), 0)
        self.assertEqual(records, before)
        self.assertEqual(len([c for c in self.backend.calls if c[0] == "mount"]), 1)
        self.backend.busy.clear()
        self.assertEqual(self.execute({}, records), 0)

    def test_external_mount_replacement_is_not_unmounted(self):
        records = self.mount_initial()
        self.backend.actual[str(self.target)][0]["id"] = "999"
        with self.assertRaises(m.MountError):
            self.execute({}, records)
        self.assertEqual(len(self.backend.calls), 1)

    def test_unmanaged_mount_is_never_adopted(self):
        self.backend.actual[str(self.target)] = [identity(self.target, "someone-else")]
        with self.assertRaises(m.MountError):
            self.execute(self.desired(), {})
        self.assertFalse(self.backend.calls)

    def test_stacked_mount_is_never_unmounted(self):
        records = self.mount_initial()
        self.backend.actual[str(self.target)].append(identity(self.target, "another", "99"))
        with self.assertRaises(m.MountError):
            self.execute({}, records)

    def test_unmanaged_child_mount_blocks_parent_removal(self):
        records = self.mount_initial()
        child = self.target / "child"
        self.backend.actual[str(child)] = [identity(child, "another", "99")]
        with self.assertRaises(m.MountError):
            self.execute({}, records)

    def test_nonempty_target_does_not_hide_files(self):
        self.target.mkdir()
        (self.target / "important").write_text("keep")
        with self.assertRaises(m.MountError):
            self.execute(self.desired(), {})

    def test_missing_mount_is_recreated(self):
        records = self.mount_initial()
        self.backend.actual.clear()
        self.assertEqual(self.execute(self.desired(), records), 0)
        self.assertEqual(len(self.backend.calls), 2)

    def test_interrupted_native_mount_requires_manual_confirmation(self):
        records = self.mount_initial()
        records[str(self.target)]["mount"] = None
        with self.assertRaises(m.MountError):
            self.execute(self.desired(), records)
        self.assertEqual(len(self.backend.calls), 1)
        self.backend.unmount(str(self.target))
        self.assertEqual(self.execute(self.desired(), records), 0)
        self.assertIsNotNone(records[str(self.target)]["mount"])

    def test_legacy_interrupted_mount_can_be_removed_using_its_unique_token(self):
        records = self.mount_legacy()
        records[str(self.target)]["mount"] = None
        self.assertEqual(self.execute({}, records), 0)
        self.assertFalse(records)

    def test_existing_fuse_mount_is_replaced_by_native_bind(self):
        records = self.mount_legacy()
        self.assertEqual(self.execute(self.desired(), records), 0)
        self.assertEqual(self.backend.calls, [
            ("unmount", str(self.target)), ("mount", str(self.source), str(self.target)),
        ])
        self.assertEqual(records[str(self.target)]["backend"], "bind")
        self.assertNotIn("token", records[str(self.target)])
        self.assertEqual(records[str(self.target)]["mount"]["fstype"], "ext4")

    def test_busy_legacy_mount_is_not_replaced(self):
        records = self.mount_legacy()
        before = copy.deepcopy(records)
        self.backend.busy.add(str(self.target))
        self.assertGreater(self.execute(self.desired(), records), 0)
        self.assertEqual(records, before)
        self.assertEqual(self.backend.calls, [("unmount", str(self.target))])

    def test_unrecorded_native_mount_is_not_adopted(self):
        self.backend.actual[str(self.target)] = [native_identity(self.target, self.source)]
        self.backend.sources[str(self.target)] = str(self.source)
        with self.assertRaises(m.MountError):
            self.execute(self.desired(), {})

    def test_native_keep_checks_source_identity(self):
        records = self.mount_initial()
        self.backend.sources[str(self.target)] = str(self.other)
        self.assertEqual(self.execute(self.desired(), records), 1)
        self.assertEqual(len(self.backend.calls), 1)

    def test_new_mount_must_refer_to_same_file(self):
        records = {}
        with patch.object(self.backend, "samefile", return_value=False):
            self.assertEqual(self.execute(self.desired(), records), 1)
        self.assertIsNone(records[str(self.target)]["mount"])

    def test_mount_failure_does_not_record_success(self):
        records = {}
        self.backend.fail_mount = True
        self.assertEqual(self.execute(self.desired(), records), 1)
        self.assertEqual(records, {})
        self.assertEqual(self.store.saved, {})

    def test_failed_state_write_prevents_mount(self):
        with patch.object(self.store, "save", side_effect=OSError("disk full")):
            self.assertEqual(self.execute(self.desired(), {}), 1)
        self.assertFalse(self.backend.calls)

    def test_plan_rejects_source_in_mount_that_will_be_removed(self):
        records = self.mount_initial()
        desired = {str(self.project / "new"): str(self.target / "subdir")}
        with self.assertRaises(m.MountError):
            m.make_plan(desired, records, self.backend.mounts())

    def test_changes_after_plan_are_checked_again(self):
        plan = m.make_plan(self.desired(), {}, {})
        self.backend.actual[str(self.target)] = [identity(self.target, "another")]
        self.assertEqual(m.execute_plan(plan, {}, self.store, self.backend, emit=lambda _: None), 1)
        self.assertFalse(self.backend.calls)

    def test_state_roundtrip_and_corruption(self):
        records = self.mount_initial()
        store = m.StateStore(self.config, self.base / "state")
        with store.lock():
            store.save(records)
            self.assertEqual(store.load(), records)
        store.path.write_text("broken")
        with self.assertRaises(m.MountError):
            store.load()

    def write_legacy_state(self, config, records):
        store = m.StateStore(config)
        store.directory.mkdir(parents=True, exist_ok=True)
        key = m.hashlib.sha256(str(config).encode()).hexdigest()
        path = store.directory / (key + ".json")
        path.write_text(json.dumps({"version": 1, "config": str(config), "records": records}))
        return path

    def test_config_rename_keeps_same_state_path(self):
        old = m.StateStore(self.config)
        renamed = m.StateStore(self.project / "mounts_112.json")
        self.assertEqual(old.path, renamed.path)

    def test_different_projects_keep_separate_state_with_shared_base(self):
        base = self.base / "state"
        first = m.StateStore(self.config, base)
        second = m.StateStore(self.base / "project2/mounts.json", base)
        self.assertNotEqual(first.path, second.path)

    def test_rename_recovers_and_migrates_legacy_records_without_remount(self):
        records = self.mount_initial()
        legacy = self.write_legacy_state(self.config, records)
        self.write_config()
        renamed = self.project / "mounts_112.json"
        self.config.rename(renamed)
        with patch.object(m, "SystemBackend", return_value=self.backend), patch("builtins.print"):
            self.assertEqual(m.main(["sync", str(renamed)]), 0)
        store = m.StateStore(renamed)
        self.assertEqual(store.load(), records)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertFalse(legacy.exists())
        data = json.loads(store.path.read_text())
        self.assertEqual(data["version"], 3)
        self.assertEqual(data["config"], str(renamed))

    def test_rename_dry_run_reads_legacy_without_migrating(self):
        records = self.mount_initial()
        legacy = self.write_legacy_state(self.config, records)
        before = legacy.read_bytes()
        self.write_config()
        renamed = self.project / "mounts_112.json"
        self.config.rename(renamed)
        with patch.object(m, "SystemBackend", return_value=self.backend), patch("builtins.print"):
            self.assertEqual(m.main(["sync", str(renamed), "--dry-run"]), 0)
        self.assertEqual(legacy.read_bytes(), before)
        self.assertFalse(m.StateStore(renamed).path.exists())
        self.assertEqual(len(self.backend.calls), 1)

    def test_renamed_empty_config_removes_previously_managed_mounts(self):
        records = self.mount_initial()
        legacy = self.write_legacy_state(self.config, records)
        self.write_config([])
        renamed = self.project / "mounts_112.json"
        self.config.rename(renamed)
        with patch.object(m, "SystemBackend", return_value=self.backend), patch("builtins.print"):
            self.assertEqual(m.main(["sync", str(renamed)]), 0)
        self.assertEqual(m.StateStore(renamed).load(), {})
        self.assertFalse(legacy.exists())
        self.assertEqual(self.backend.calls[-1], ("unmount", str(self.target)))

    def test_conflicting_legacy_records_do_not_get_silently_merged(self):
        records = self.mount_initial()
        self.write_legacy_state(self.config, records)
        other = copy.deepcopy(records)
        other[str(self.target)]["source"] = str(self.other)
        self.write_legacy_state(self.project / "other.json", other)
        store = m.StateStore(self.project / "renamed.json")
        with self.assertRaises(m.MountError):
            store.load()
        self.assertFalse(store.path.exists())

    def test_committed_state_wins_if_legacy_cleanup_was_interrupted(self):
        records = self.mount_initial()
        store = m.StateStore(self.config)
        with store.lock():
            store.save({})
        legacy = self.write_legacy_state(self.config, records)
        with store.lock():
            self.assertEqual(store.load(), {})
            store.save({})
        self.assertFalse(legacy.exists())

    def test_version_two_fuse_state_can_be_loaded_for_migration(self):
        records = self.mount_legacy()
        store = m.StateStore(self.config)
        with store.lock():
            store.path.write_text(json.dumps({
                "version": 2, "project": str(self.project),
                "config": str(self.config), "records": records,
            }))
            self.assertEqual(store.load(), records)

    def test_backend_uses_only_native_mount_commands(self):
        backend = m.SystemBackend()
        with patch.object(m.os, "geteuid", return_value=2015), \
                patch.object(backend, "check_source"), patch.object(backend, "run") as run:
            backend.mount(str(self.source), str(self.target))
            backend.unmount(str(self.target))
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["sudo", "-n", "mount", "--bind", "--", str(self.source), str(self.target)],
            ["sudo", "-n", "umount", "--", str(self.target)],
        ])

    def test_backend_rejects_fuse_source(self):
        backend = m.SystemBackend()
        mounts = {str(self.source): [identity(self.source, "some-fuse")]}
        with patch.object(backend, "mounts", return_value=mounts), self.assertRaises(m.MountError):
            backend.check_source(str(self.source))

    def test_sudo_failure_happens_before_any_mount_change(self):
        backend = m.SystemBackend()
        actions = [m.Action("unmount", str(self.target))]
        with patch.object(m.os, "geteuid", return_value=2015), \
                patch.object(m.shutil, "which", side_effect=lambda name: "/usr/bin/" + name), \
                patch.object(m.subprocess, "run", return_value=m.subprocess.CompletedProcess([], 1)), \
                patch.object(backend, "privileged_run") as run, self.assertRaises(m.MountError):
            backend.preflight(actions)
        run.assert_not_called()

    def test_passwordless_sudo_does_not_require_sudo_validate(self):
        backend = m.SystemBackend()
        with patch.object(m.os, "geteuid", return_value=2015), \
                patch.object(m.shutil, "which", side_effect=lambda name: "/usr/bin/" + name), \
                patch.object(m.subprocess, "run", return_value=m.subprocess.CompletedProcess([], 0)) as run:
            backend.preflight([m.Action("unmount", str(self.target))])
        self.assertEqual([call.args[0] for call in run.call_args_list], [["sudo", "-n", "true"]])

    def test_default_state_is_beside_config_even_with_xdg_override(self):
        with patch.dict("os.environ", {"XDG_STATE_HOME": str(self.base / "elsewhere")}):
            store = m.StateStore(self.config)
        self.assertEqual(store.base, self.project / ".mountctl")
        self.assertIn(self.project / ".mountctl", store.path.parents)
        self.assertFalse(store.base.exists())
        with store.lock():
            store.save({})
        self.assertEqual(store.load(), {})

    def test_explicit_state_directory_is_still_supported(self):
        base = self.base / "custom-state"
        store = m.StateStore(self.config, base)
        self.assertEqual(store.base, base)
        self.assertIn(base, store.path.parents)

    def test_mount_target_cannot_cover_or_enter_state_directory(self):
        store = m.StateStore(self.config)
        for target in [store.base, store.base / "sdk", self.project]:
            with self.subTest(target=target), self.assertRaises(m.MountError):
                store.check_targets([str(target)])
        store.check_targets([str(self.target)])

    def test_state_target_conflict_fails_before_creating_state(self):
        self.write_config([{"source": "../sdk", "target": ".mountctl/sdk"}])
        with patch("builtins.print"):
            self.assertEqual(m.main(["sync", str(self.config)]), 1)
        self.assertFalse((self.project / ".mountctl").exists())

    def test_default_dry_run_leaves_project_without_state_directory(self):
        self.write_config()
        with patch("builtins.print"):
            self.assertEqual(m.main(["sync", str(self.config), "--dry-run"]), 0)
        self.assertFalse((self.project / ".mountctl").exists())

    def test_project_state_is_isolated_by_uid(self):
        with patch.object(m.os, "getuid", return_value=1000):
            first = m.StateStore(self.config).path
        with patch.object(m.os, "getuid", return_value=2015):
            second = m.StateStore(self.config).path
        self.assertNotEqual(first, second)

    def test_state_is_isolated_by_host_boot_and_namespace(self):
        paths = []
        for scope in [("host1", "boot1", "ns1"), ("host2", "boot1", "ns1"),
                      ("host1", "boot2", "ns1"), ("host1", "boot1", "ns2")]:
            with patch.object(m, "scope_id", return_value=scope):
                paths.append(m.StateStore(self.config, self.base / "state").path)
        self.assertEqual(len(set(paths)), 4)

    def test_parallel_sync_is_locked(self):
        store = m.StateStore(self.config, self.base / "state")
        with store.lock():
            with self.assertRaises(m.MountError):
                with store.lock():
                    pass

    def test_dry_run_does_not_create_target_or_state(self):
        self.write_config()
        state = self.base / "state"
        with patch("builtins.print"):
            self.assertEqual(m.main(["sync", str(self.config), "--dry-run", "--state-dir", str(state)]), 0)
        self.assertFalse(state.exists())
        self.assertFalse(self.target.exists())

    def test_missing_config_does_not_clear_existing_mounts(self):
        records = self.mount_initial()
        with patch("builtins.print"):
            self.assertEqual(m.main(["sync", str(self.config)]), 1)
        self.assertTrue(records)
        self.assertEqual(len(self.backend.calls), 1)


if __name__ == "__main__":
    unittest.main()
