import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from spellguard.analysis import parse_snapshot
from spellguard.cli import main
from spellguard.models import CollectedSnapshot, Coverage, Snapshot, SourceFile
from spellguard.repository import collect_commit, collect_working_tree
from spellguard.review import compare_findings
from spellguard.rules import detect_findings


CHAIN = 'if x == 1 { return 10 } else if x == 2 { return 20 } else { return 30 }'


def functions(*names, body=CHAIN):
    return 'package sample\n' + '\n'.join(
        f'func {name}(x int) int {{ {body} }}' for name in names
    ) + '\n'


def analyze(**files):
    sources = tuple(
        SourceFile(path, text, hashlib.sha256(text.encode()).hexdigest())
        for path, text in files.items()
    )
    return parse_snapshot(CollectedSnapshot(
        Snapshot('test', 'working-tree', sources),
        Coverage(len(sources), len(sources), 0, 0, 0, (), True),
    ))


def findings(source):
    result = analyze(**{'sample.go': source})
    if not result.coverage.complete:
        raise AssertionError(result.coverage.diagnostics)
    return detect_findings(result.facts)


class GoAnalysisTest(unittest.TestCase):
    def test_repeated_chains_three_positive_forms(self):
        bodies = [CHAIN,
                  'if y := x; y == 1 { return 1 } else if y == 2 { return 2 }; return 0',
                  'if x < 1 { return 1 } else if x > 2 { return 2 } else { return 0 }']
        for body in bodies:
            with self.subTest(body=body):
                items = findings(functions('First', 'Second', body=body))
                self.assertEqual([f.rule_id for f in items], ['SG002'])
                self.assertEqual(items[0].group_members, ('First', 'Second'))
                self.assertEqual(len(items[0].related_locations), 1)

    def test_repeated_chains_negative_boundaries(self):
        cases = [functions('First', 'Second', body='if x != 0 { return x }; return 0'),
                 functions('First') + functions('Second', body=CHAIN.replace('20', '99')).split('\n', 1)[1],
                 'package p\nfunc F(x int) int {' + CHAIN + ';' + CHAIN + '}\n',
                 functions('First', 'Second', body='if x==1 { return 1 } else { if x==2 { return 2 } }; return 0')]
        for source in cases:
            with self.subTest(source=source):
                self.assertFalse(any(f.rule_id == 'SG002' for f in findings(source)))

    def test_comments_whitespace_and_function_order_do_not_change_group(self):
        before = findings(functions('First', 'Second'))[0]
        after = findings(functions('Second', 'First', body=CHAIN.replace('x == 1', '(x == 1)').replace('return 10', '/* comment */ return 10')))[0]
        self.assertEqual(before.fingerprint, after.fingerprint)
        self.assertEqual(len(compare_findings([before], [after]).persisting), 1)

    def test_string_contents_are_not_normalized_as_comments(self):
        body = 'if x==1 { println("a // b"); return 1 } else if x==2 { return 2 }; return 0'
        source = functions('First', body=body) + functions('Second', body=body.replace('a // b', 'a b')).split('\n', 1)[1]
        self.assertFalse(any(f.rule_id == 'SG002' for f in findings(source)))

    def test_method_receiver_distinguishes_same_name(self):
        source = 'package p\ntype A struct{}\ntype B struct{}\n'
        source += f'func (a *A) Check(x int) int {{ {CHAIN} }}\n'
        source += f'func (b B) Check(x int) int {{ {CHAIN} }}\n'
        item = findings(source)[0]
        self.assertEqual(item.group_members, ('A.Check', 'B.Check'))

    def test_nested_literal_cases_three_positive_forms(self):
        for condition in ['kind == "legacy"', 'kind == 7', 'kind == "legacy" && region == "EU"']:
            with self.subTest(condition=condition):
                source = f'package p\nfunc F() {{ if {condition} {{ if tier == "gold" {{ work() }} }} }}\n'
                items = findings(source)
                self.assertEqual([f.rule_id for f in items], ['SG003'])
                self.assertEqual(len(items[0].related_locations), 1)

    def test_nested_case_negative_boundaries(self):
        bodies = [
            'if err == nil { if tier == "gold" { work() } }',
            'if kind == "x" || region == "y" { if tier == "gold" { work() } }',
            'if kind == "x" { for { if tier == "gold" { work() } } }',
            'if kind == "x" { f := func() { if tier == "gold" { work() } }; f() }',
            'if x := read(); x == "x" { if tier == "gold" { work() } }',
            'if read() == "x" { if tier == "gold" { work() } }',
            'if kind == "x" { work() } else if tier == "gold" { work() }',
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assertFalse(any(f.rule_id == 'SG003' for f in findings('package p\nfunc F(){' + body + '}\n')))

    def test_closures_are_not_misattributed_to_named_functions(self):
        body = f'f := func(x int) int {{ {CHAIN} }}; return f(x)'
        self.assertEqual(findings(functions('First', 'Second', body=body)), ())

    def test_ordinary_errors_and_switch_do_not_trigger(self):
        source = 'package p\nfunc F() error { if err := work(); err != nil { return err }; return nil }\nfunc G(x int) int { switch x { case 1: return 1; default: return 0 } }\n'
        self.assertEqual(findings(source), ())

    def test_generics_build_tags_and_unicode_parse(self):
        source = '//go:build windows\n\npackage p\nfunc Identity[T any](x T) T { return x }\nfunc 中文() { if kind == "旧" { if region == "中" { work() } } }\n'
        result = analyze(**{'modern_test.go': source})
        self.assertTrue(result.coverage.complete)
        self.assertEqual([f.rule_id for f in detect_findings(result.facts)], ['SG003'])
        self.assertTrue(all(f.syntax_model_version.startswith('go-tree-sitter-') for f in result.facts))

    def test_locations_use_source_offsets_for_long_files(self):
        source = 'package p\n' + ('// filler\n' * 1000)
        source += 'func F() { if code == 99991663 { if code == 0 { work() } } }\n'
        result = analyze(**{'long.go': source})
        fact = result.facts[0]
        self.assertEqual(fact.source_range.start_line, 1002)
        self.assertEqual(fact.source_range.end_line, 1002)
        self.assertEqual(fact.related_ranges[0].start_line, 1002)
        self.assertLessEqual(fact.source_range.end_line, len(source.splitlines()))

    def test_broken_go_preserves_valid_python_and_go(self):
        result = analyze(**{'bad.go': 'package p\nfunc F( {', 'ok.go': functions('A', 'B'),
                            'ok.py': 'def f():\n try: work()\n except OSError: return 0\n'})
        self.assertEqual((result.coverage.analyzed_files, result.coverage.failed_files), (2, 1))
        self.assertFalse(result.coverage.complete)
        self.assertEqual({f.rule_id for f in detect_findings(result.facts)}, {'SG001', 'SG002'})
        self.assertEqual(result.coverage.diagnostics[0].code, 'GO_SYNTAX_ERROR')

    def test_missing_nodes_and_package_fail_instead_of_recovery_success(self):
        for source in ['', 'func F() {}', 'package p\nfunc F() { if x == 1 { return }']:
            with self.subTest(source=source):
                result = analyze(**{'bad.go': source})
                self.assertFalse(result.coverage.complete)
                self.assertEqual(result.facts, ())


class GoCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), '-c', 'core.hooksPath=/dev/null', *args])

    def write(self, source):
        (self.root / 'sample.go').write_text(source)

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')

    def run_cli(self, *args):
        before = Path.cwd()
        output = io.StringIO()
        try:
            os.chdir(self.root)
            with contextlib.redirect_stdout(output):
                code = main([*args, '--format', 'json'])
        finally:
            os.chdir(before)
        return code, json.loads(output.getvalue())

    def test_go_only_three_commands_scope_and_readonly(self):
        self.write(functions('A', 'B'))
        (self.root / 'go.mod').write_text('module example.invalid/sample\ngo 1.23\n')
        (self.root / 'init.go').write_text('package p\nimport "os"\nfunc init(){ os.WriteFile("EXECUTED", nil, 0600) }\n')
        self.commit()
        status = self.git('status', '--porcelain=v1', '-z')
        for args in [('scan',), ('debt',), ('review', '--base', 'HEAD')]:
            with self.subTest(args=args):
                code, report = self.run_cli(*args)
                self.assertEqual(code, 0)
                self.assertEqual(report['analysis_scope']['go']['rules'], ['SG002', 'SG003'])
                self.assertIn('switch', ' '.join(report['analysis_scope']['go']['limitations']))
                self.assertEqual(report['ruleset_version'], 'candidate-5')
        self.assertEqual(status, self.git('status', '--porcelain=v1', '-z'))
        self.assertFalse((self.root / 'EXECUTED').exists())

    def test_group_growth_and_old_acceptance_do_not_approve_new_member(self):
        self.write(functions('A', 'B'))
        self.commit()
        _, scan = self.run_cli('scan')
        old = scan['findings'][0]
        entry = dict(fingerprint=old['fingerprint'], rule_id='SG002', reason='reviewed', owner='test', created_at='2026-01-01', expires_at='2099-01-01')
        (self.root / '.spellguard-exceptions.json').write_text(json.dumps({'schema_version': 1, 'exceptions': [entry]}))
        self.write(functions('A', 'B', 'C'))
        code, report = self.run_cli('review', '--base', 'HEAD', '--fail-on', 'medium')
        self.assertEqual(code, 1)
        self.assertEqual(report['delta']['introduced'][0]['reason'], 'group-expanded')
        self.assertEqual(report['delta']['introduced'][0]['after']['governance_status'], 'unregistered')
        self.assertEqual(len(report['governance']['unmatched']), 1)
        self.write(functions('A'))
        code, report = self.run_cli('review', '--base', 'HEAD')
        self.assertEqual(code, 0)
        self.assertEqual(len(report['delta']['resolved']), 1)

    def test_go_rename_delete_and_failures(self):
        self.write(functions('A', 'B'))
        self.commit()
        self.git('mv', 'sample.go', 'renamed.go')
        code, report = self.run_cli('review', '--base', 'HEAD')
        self.assertEqual(code, 0)
        self.assertEqual(report['delta']['persisting'][0]['reason'], 'explicit-rename')
        self.git('rm', '-f', 'renamed.go')
        code, report = self.run_cli('review', '--base', 'HEAD')
        self.assertEqual(code, 0)
        self.assertEqual(len(report['delta']['resolved']), 1)
        (self.root / 'bad.go').write_text('package p\nfunc F( {')
        code, report = self.run_cli('review', '--base', 'HEAD', '--fail-on', 'medium')
        self.assertEqual(code, 2)
        self.assertFalse(report['complete'])
        self.assertEqual(report['delta']['resolved'], [])
        self.assertIn('GO_SYNTAX_ERROR', [d['code'] for d in report['diagnostics']])

    def test_go_decoding_symlinks_and_commit_collection(self):
        self.write(functions('A', 'B'))
        (self.root / 'bad.go').write_bytes(b'// coding: latin-1\npackage p\n//\xff\n')
        (self.root / 'link.go').symlink_to('/definitely/not/part/of/repo.go')
        (self.root / 'variant_test.go').write_text('//go:build other\n\npackage p\n')
        self.commit()
        for collected in [collect_working_tree(self.root), collect_commit(self.root, 'HEAD')]:
            self.assertEqual(collected.coverage.analyzed_files, 2)
            self.assertEqual(collected.coverage.failed_files, 1)
            self.assertEqual(collected.coverage.excluded_files, 1)
            self.assertIn('SOURCE_READ_FAILED', [d.code for d in collected.coverage.diagnostics])

    def test_go_bad_registry_does_not_become_empty_success(self):
        self.write(functions('A', 'B'))
        self.commit()
        (self.root / '.spellguard-exceptions.json').write_text('{bad')
        for args in [('scan',), ('debt',), ('review', '--base', 'HEAD')]:
            code, report = self.run_cli(*args)
            self.assertEqual(code, 2)
            self.assertEqual(report['diagnostics'][0]['code'], 'GOVERNANCE_ERROR')

    def test_staged_new_go_file_missing_is_a_visible_read_failure(self):
        self.write(functions('A', 'B'))
        self.commit()
        self.git('mv', 'sample.go', 'renamed.go')
        (self.root / 'renamed.go').unlink()
        code, report = self.run_cli('review', '--base', 'HEAD')
        self.assertEqual(code, 2)
        self.assertIn('FILE_STAT_FAILED', [d['code'] for d in report['diagnostics']])

    def test_go_acceptance_expiry_and_ambiguity_share_governance(self):
        self.write(functions('A'))
        self.commit()
        self.write(functions('A', 'B'))
        _, report = self.run_cli('scan')
        entry = dict(fingerprint=report['findings'][0]['fingerprint'], rule_id='SG002',
                     reason='reviewed', owner='test', created_at='2020-01-01', expires_at='2099-01-01')
        registry = self.root / '.spellguard-exceptions.json'
        registry.write_text(json.dumps({'schema_version': 1, 'exceptions': [entry]}))
        for args in [('scan',), ('debt',), ('review', '--base', 'HEAD', '--fail-on', 'medium')]:
            code, report = self.run_cli(*args)
            self.assertEqual(code, 0)
            self.assertEqual({s['status'] for s in report['governance']['statuses']}, {'accepted'})
        entry['expires_at'] = '2021-01-01'
        registry.write_text(json.dumps({'schema_version': 1, 'exceptions': [entry]}))
        code, report = self.run_cli('review', '--base', 'HEAD', '--fail-on', 'medium')
        self.assertEqual(code, 1)
        self.assertEqual(report['delta']['introduced'][0]['after']['governance_status'], 'expired')
        registry.unlink()
        case = 'if kind=="x" { if tier=="gold" { work() } }'
        self.write('package p\nfunc F(){ ' + case + ';' + case + ' }\n')
        self.commit()
        self.write('package p\nfunc F(){ ' + case + ';' + case + ';' + case + ' }\n')
        code, report = self.run_cli('review', '--base', 'HEAD', '--fail-on', 'medium')
        self.assertEqual(code, 0)
        self.assertEqual(len(report['delta']['unverified']), 5)
        self.assertEqual(report['delta']['introduced'], [])
