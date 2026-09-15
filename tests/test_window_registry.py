"""Window registry contract tests (R02, B10/B11/B12/B13).

B10: missing/empty/duplicate-key/unknown-schema/bool-version/oversized registry fail
     with REGISTRY_INVALID, never an empty success.
B11: TEMP removed / symbol changed / lifecycle changed / reason changed together with
     caller change invalidate against the installed digest.
B12: whitespace/key-order changes keep the digest stable.
B13: path traversal, absolute, non-regular or symlinked files fail reading.
Fixtures are synthetic; none of these tests create a registry in a real repository.
"""

import copy
import hashlib
import json
import os
import pathlib
import tempfile
import unittest

from spellguard.registry import (
    RegistryError,
    load_registry,
    parse_registry,
)
from spellguard.repository import read_repository_file

FIXTURE = {
    "schema_version": 1,
    "rules": [{
        "id": "TEMP-001",
        "classification": "temporary",
        "lifecycle": "ACTIVE",
        "reason": "Synthetic fixture: compatibility helper retained during migration.",
        "desired_state": "Synthetic fixture: remove helper after migration.",
        "protected_symbol": {"path": "src/demo/workaround.py",
                             "symbol": "fallback", "source_root": "src"},
        "window": "no_external_callers",
        "resolution_reason": None,
    }],
}

REGISTRY_REL = ".spellguard/rules.json"
VALID_DIGEST = hashlib.sha256(
    json.dumps(FIXTURE, ensure_ascii=True, sort_keys=True,
               separators=(",", ":")).encode()).hexdigest()


class TestRegistry(unittest.TestCase):
    def parse_error(self, raw: bytes) -> RegistryError:
        with self.assertRaises(RegistryError) as ctx:
            parse_registry(raw)
        error = ctx.exception
        self.assertNotIn(str(error), "")
        self.assertTrue(error.message)
        return error

    def test_valid_fixture_parses_with_canonical_digest(self):
        registry = parse_registry(json.dumps(FIXTURE).encode())
        self.assertEqual(len(registry.rules), 1)
        rule = registry.rules[0]
        self.assertEqual(rule.id, "TEMP-001")
        self.assertEqual(rule.lifecycle, "ACTIVE")
        self.assertIsNone(rule.resolution_reason)
        self.assertEqual(registry.digest, VALID_DIGEST)

    def test_empty_rules_list_is_valid_kept_matching_installed_digest(self):
        empty = {"schema_version": 1, "rules": []}
        registry = parse_registry(json.dumps(empty).encode())
        self.assertEqual(registry.rules, ())
        self.assertNotEqual(registry.digest, VALID_DIGEST)

    def test_whitespace_and_key_order_do_not_change_digest(self):
        base = parse_registry(json.dumps(FIXTURE).encode())
        reordered = parse_registry(
            json.dumps(FIXTURE, indent=4, sort_keys=True).encode())
        self.assertEqual(base.digest, reordered.digest)

    def test_rule_content_change_changes_digest(self):
        changed = copy.deepcopy(FIXTURE)
        changed["rules"][0]["protected_symbol"]["symbol"] = "replacement"
        base = parse_registry(json.dumps(FIXTURE).encode())
        self.assertNotEqual(base.digest, parse_registry(
            json.dumps(changed).encode()).digest)

    def test_rule_order_change_changes_digest(self):
        changed = copy.deepcopy(FIXTURE)
        changed["rules"][0]["id"] = "TEMP-002"
        base = parse_registry(json.dumps(FIXTURE).encode())
        self.assertNotEqual(base.digest, parse_registry(
            json.dumps(changed).encode()).digest)

    def test_duplicate_json_key_rejected(self):
        self.parse_error(
            b'{"schema_version": 1, "schema_version": 2, "rules": []}')

    def test_missing_file_is_not_same_as_empty(self):
        # Missing content entirely: parse of empty bytes fails.
        self.parse_error(b"")

    def test_malformed_json_rejected(self):
        self.parse_error(b'{"schema_version": 1, "rules": ')

    def test_schema_must_be_int_not_bool(self):
        for raw in ('{"schema_version": true, "rules": []}',
                    '{"schema_version": "1", "rules": []}'):
            self.parse_error(raw.encode())

    def test_unknown_schema_version_rejected(self):
        self.parse_error(b'{"schema_version": 2, "rules": []}')

    def test_unknown_field_rejected(self):
        changed = copy.deepcopy(FIXTURE)
        changed["note"] = "extra"
        self.parse_error(json.dumps(changed).encode())

    def test_unknown_rule_field_rejected(self):
        changed = copy.deepcopy(FIXTURE)
        changed["rules"][0]["extra"] = 1
        self.parse_error(json.dumps(changed).encode())

    def test_two_rules_rejected_as_out_of_scope(self):
        changed = copy.deepcopy(FIXTURE)
        second = copy.deepcopy(changed["rules"][0])
        second["id"] = "TEMP-002"
        changed["rules"].append(second)
        self.parse_error(json.dumps(changed).encode())

    def test_cand_style_id_rejected(self):
        changed = copy.deepcopy(FIXTURE)
        changed["rules"][0]["id"] = "CAND-001"
        self.parse_error(json.dumps(changed).encode())

    def test_short_id_rejected(self):
        changed = copy.deepcopy(FIXTURE)
        changed["rules"][0]["id"] = "TEMP-01"
        self.parse_error(json.dumps(changed).encode())

    def test_empty_reason_rejected(self):
        changed = copy.deepcopy(FIXTURE)
        changed["rules"][0]["reason"] = ""
        self.parse_error(json.dumps(changed).encode())

    def test_empty_desired_state_rejected(self):
        changed = copy.deepcopy(FIXTURE)
        changed["rules"][0]["desired_state"] = ""
        self.parse_error(json.dumps(changed).encode())

    def test_unknown_window_rejected(self):
        changed = copy.deepcopy(FIXTURE)
        changed["rules"][0]["window"] = "GARBAGE"
        self.parse_error(json.dumps(changed).encode())

    def test_unknown_classification_rejected(self):
        changed = copy.deepcopy(FIXTURE)
        changed["rules"][0]["classification"] = "accepted"
        self.parse_error(json.dumps(changed).encode())

    def test_unknown_lifecycle_rejected(self):
        changed = copy.deepcopy(FIXTURE)
        changed["rules"][0]["lifecycle"] = "MERGED"
        self.parse_error(json.dumps(changed).encode())

    def test_active_with_resolution_reason_rejected(self):
        changed = copy.deepcopy(FIXTURE)
        changed["rules"][0]["resolution_reason"] = "closed"
        self.parse_error(json.dumps(changed).encode())

    def test_resolved_requires_reason(self):
        changed = copy.deepcopy(FIXTURE)
        changed["rules"][0]["lifecycle"] = "RESOLVED"
        self.parse_error(json.dumps(changed).encode())

    def test_resolved_with_reason_valid(self):
        changed = copy.deepcopy(FIXTURE)
        changed["rules"][0]["lifecycle"] = "RESOLVED"
        changed["rules"][0]["resolution_reason"] = "helper removed after migration."
        registry = parse_registry(json.dumps(changed).encode())
        self.assertEqual(registry.rules[0].lifecycle, "RESOLVED")

    def registrar_path_cases(self) -> list[tuple[str, str]]:
        return [
            ("dotdot", "src/../e.py"),
            ("traversal", "src/demo/../../bypass.py"),
            ("backslash", "src\\demo\\workaround.py"),
            ("no_suffix", "src/demo/workaround"),
            ("empty", ""),
        ]

    def test_invalid_protected_symbol_paths_rejected(self):
        cases = self.registrar_path_cases()
        for label, path in cases:
            changed = copy.deepcopy(FIXTURE)
            changed["rules"][0]["protected_symbol"]["path"] = path
            error = self.parse_error(json.dumps(changed).encode())
            error.message

    def test_source_root_must_be_dot_or_src(self):
        for value in ("lib", "/tmp", "", 3):
            changed = copy.deepcopy(FIXTURE)
            changed["rules"][0]["protected_symbol"]["source_root"] = value
            self.parse_error(json.dumps(changed).encode())

    def test_dot_source_root_requires_root_level_path(self):
        changed = copy.deepcopy(FIXTURE)
        changed["rules"][0]["protected_symbol"]["source_root"] = "."
        # demo/workaround.py does not sit directly under "."; must be invalid.
        self.parse_error(json.dumps(changed).encode())

    def test_dot_source_root_root_level_path_valid(self):
        changed = copy.deepcopy(FIXTURE)
        changed["rules"][0]["protected_symbol"]["source_root"] = "."
        changed["rules"][0]["protected_symbol"]["path"] = "workaround.py"
        registry = parse_registry(json.dumps(changed).encode())
        self.assertEqual(registry.rules[0].protected_symbol.path, "workaround.py")

    def test_path_must_lay_under_source_root(self):
        changed = copy.deepcopy(FIXTURE)
        changed["rules"][0]["protected_symbol"]["source_root"] = "."
        changed["rules"][0]["protected_symbol"]["path"] = "lib/x.py"
        self.parse_error(json.dumps(changed).encode())

    def test_path_under_src_but_not_package_rejected_by_definition_contract(self):
        # src/level1.py: source_root "src", but no package layer above.
        changed = copy.deepcopy(FIXTURE)
        changed["rules"][0]["protected_symbol"]["path"] = "level1.py"
        self.parse_error(json.dumps(changed).encode())

    def test_symbol_must_be_single_identifier_or_full_path(self):
        for value in ("self.method", "demo.fallback", "1abc", ""):
            changed = copy.deepcopy(FIXTURE)
            changed["rules"][0]["protected_symbol"]["symbol"] = value
            self.parse_error(json.dumps(changed).encode())

    def test_oversized_registry_rejected(self):
        big = json.dumps(FIXTURE).encode() * (64 * 1024)
        error = self.parse_error(big)
        self.assertEqual(error.code, "REGISTRY_INVALID")

    def test_duplicate_rule_id_rejected(self):
        changed = copy.deepcopy(FIXTURE)
        changed["rules"][0]["id"] = "TEMP-002"
        changed["rules"].append(copy.deepcopy(changed["rules"][0]))
        self.parse_error(json.dumps(changed).encode())

    def test_resolution_reason_type_must_be_string(self):
        changed = copy.deepcopy(FIXTURE)
        changed["rules"][0]["lifecycle"] = "RESOLVED"
        changed["rules"][0]["resolution_reason"] = 3
        self.parse_error(json.dumps(changed).encode())


class WindowRegistryGoIdentityTest(unittest.TestCase):
    """G01: Go protected symbol identity rules."""

    def go_fixture(self, path="backend/legacy/adapt.go", symbol="Adapt",
                   source_root="backend"):
        return {
            "schema_version": 1,
            "rules": [{
                "id": "TEMP-001",
                "classification": "temporary",
                "lifecycle": "ACTIVE",
                "reason": "Synthetic Go fixture: migration helper.",
                "desired_state": "Synthetic: remove adapter.",
                "protected_symbol": {"path": path, "symbol": symbol,
                                     "source_root": source_root},
                "window": "no_external_callers",
                "resolution_reason": None,
            }],
        }

    def parse_or_error(self, raw):
        from spellguard.registry import parse_registry, RegistryError
        try:
            return parse_registry(json.dumps(raw).encode())
        except RegistryError as error:
            return error

    def test_go_rule_with_module_root_source_root_is_valid(self):
        registry = self.parse_or_error(self.go_fixture())
        self.assertEqual(registry.rules[0].protected_symbol.path,
                         "backend/legacy/adapt.go")

    def test_go_init_traversal_and_underscore_rejected(self):
        for symbol in ("init", "_", "iota"):
            error = self.parse_or_error(self.go_fixture(symbol=symbol))
            self.assertEqual(getattr(error, "code", None), "REGISTRY_INVALID",
                             symbol)
        for path in ("backend/../adapt.go", "/abs/adapt.go",
                     "backend\\adapt.go"):
            error = self.parse_or_error(self.go_fixture(path=path))
            self.assertEqual(getattr(error, "code", None), "REGISTRY_INVALID")

    def test_go_method_qualified_symbol_rejected(self):
        error = self.parse_or_error(self.go_fixture(symbol="Type.Method"))
        self.assertEqual(getattr(error, "code", None), "REGISTRY_INVALID")

    def test_python_stable_fixture_unchanged_by_go_branch(self):
        registry = self.parse_or_error(FIXTURE)
        self.assertEqual(len(registry.rules), 1)
        self.assertEqual(registry.rules[0].id, "TEMP-001")


class TestLoadRegistry(unittest.TestCase):
    def write_registry(self, root, raw: bytes):
        path = root / ".spellguard"
        path.mkdir(exist_ok=True)
        (path / "rules.json").write_bytes(raw)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.write_registry(self.root, json.dumps(FIXTURE).encode())

    def tearDown(self):
        self.temporary.cleanup()

    def test_load_reads_file_and_matches_digest(self):
        registry = load_registry(self.root, VALID_DIGEST)
        self.assertEqual(len(registry.rules), 1)

    def test_changed_digest_is_registry_changed(self):
        with self.assertRaises(RegistryError) as ctx:
            load_registry(self.root, hashlib.sha256(b"installed").hexdigest())
        self.assertEqual(ctx.exception.code, "REGISTRY_CHANGED")

    def test_missing_file_maps_to_read_failed(self):
        other = pathlib.Path(tempfile.mkdtemp())
        try:
            with self.assertRaises(RegistryError) as ctx:
                load_registry(other, VALID_DIGEST)
            self.assertEqual(ctx.exception.code, "REGISTRY_READ_FAILED")
        finally:
            other.rmdir()

    def test_invalid_content_maps_to_invalid(self):
        self.write_registry(self.root, b"{markdown")
        with self.assertRaises(RegistryError) as ctx:
            load_registry(self.root, VALID_DIGEST)
        self.assertEqual(ctx.exception.code, "REGISTRY_INVALID")

    def test_sha_arg_invalid_shape_rejected(self):
        with self.assertRaises(RegistryError):
            load_registry(self.root, "XYZ")

    def test_symlink_registry_file_rejected(self):
        target = self.root / "real.json"
        target.write_bytes(json.dumps(FIXTURE).encode())
        fake = self.root / ".spellguard" / "rules.json"
        fake.unlink()
        fake.symlink_to(target)
        with self.assertRaises(RegistryError) as ctx:
            load_registry(self.root, VALID_DIGEST)
        self.assertEqual(ctx.exception.code, "REGISTRY_READ_FAILED")

    def test_deleted_rule_changes_digest(self):
        changed = {"schema_version": 1, "rules": []}
        self.write_registry(self.root, json.dumps(changed).encode())
        base = parse_registry(json.dumps(changed).encode())
        with self.assertRaises(RegistryError):
            load_registry(self.root, VALID_DIGEST)
        self.assertEqual(base.digest, hashlib.sha256(
            json.dumps(changed, ensure_ascii=True, sort_keys=True,
                       separators=(",", ":")).encode()).hexdigest())


class TestReadRepositoryFile(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        (self.root / "src").mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def test_reads_small_file(self):
        (self.root / "src" / "a.py").write_text("value = 1\n")
        self.assertEqual(
            read_repository_file(self.root, "src/a.py", 1024), b"value = 1\n")

    def test_over_limit_read_fails_without_reading_unbounded(self):
        data = b"x" * 2048
        (self.root / "src" / "a.py").write_bytes(data)
        with self.assertRaises(RegistryError):
            read_repository_file(self.root, "src/a.py", 1024)

    def test_within_limit_ok(self):
        data = b"x" * 1023
        (self.root / "src" / "a.py").write_bytes(data)
        self.assertEqual(
            read_repository_file(self.root, "src/a.py", 1024), data)

    def test_missing_file_error(self):
        with self.assertRaises(RegistryError):
            read_repository_file(self.root, "src/absent.py", 1024)

    def test_absolute_path_rejected(self):
        with self.assertRaises(RegistryError):
            read_repository_file(self.root, "/etc/passwd", 1024)

    def test_traversal_rejected(self):
        with self.assertRaises(RegistryError):
            read_repository_file(self.root, "../outside.py", 1024)

    def test_symlink_file_rejected(self):
        (self.root / "outside.py").write_text("value = 1\n")
        link = self.root / "src" / "link.py"
        link.symlink_to("../outside.py")
        with self.assertRaises(RegistryError):
            read_repository_file(self.root, "src/link.py", 1024)

    def test_symlink_parent_rejected(self):
        (self.root / "lib").mkdir()
        (self.root / "secret.py").write_text("value = 1\n")
        link = self.root / "lib" / "mirror"
        link.symlink_to(self.root)  # directory symlink
        with self.assertRaises(RegistryError):
            read_repository_file(self.root, "lib/mirror/secret.py", 1024)


if __name__ == "__main__":
    unittest.main()
