"""First-use review contracts in real temporary Git repositories."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from spellguard.cli import main


def candidates(count):
    return '\n'.join('def f{0}():\n    try: work()\n    except OSError: return {0}\n'.format(i) for i in range(count))


class FirstRunTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.previous = Path.cwd()
        os.chdir(self.root)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')

    def tearDown(self):
        os.chdir(self.previous)
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git', *args], stderr=subprocess.PIPE, text=True)

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')

    def run_cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(list(args))
        return code, output.getvalue()

    def test_default_matches_explicit_head_and_is_readonly(self):
        (self.root / 'base.py').write_text('value=1\n')
        self.commit()
        (self.root / 'base.py').write_text(candidates(1))
        self.git('add', '.')
        (self.root / 'base.py').write_text(candidates(2))
        (self.root / 'new.py').write_text(candidates(1))
        before = self.git('status', '--porcelain')
        code, output = self.run_cli('review', '--format', 'json')
        explicit = self.run_cli('review', '--base', 'HEAD', '--format', 'json')
        self.assertEqual((code, output), explicit)
        self.assertEqual(len(json.loads(output)['delta']['introduced']), 3)
        self.assertEqual(before, self.git('status', '--porcelain'))
        self.assertFalse((self.root / '.spellguard-exceptions.json').exists())

    def test_clean_scope_and_explicit_older_base(self):
        (self.root / 'base.py').write_text('value=1\n')
        self.commit()
        old = self.git('rev-parse', 'HEAD').strip()
        (self.root / 'base.py').write_text(candidates(1))
        self.commit()
        code, output = self.run_cli('review')
        self.assertEqual(code, 0)
        self.assertIn('No working-tree changes relative to this baseline', output)
        self.assertNotIn('fact:', output)
        _, older = self.run_cli('review', '--base', old, '--format', 'json')
        self.assertEqual(len(json.loads(older)['delta']['introduced']), 1)

    def test_no_head_and_invalid_base_fail_without_guessing(self):
        (self.root / 'base.py').write_text('value=1\n')
        code, output = self.run_cli('review', '--format', 'json')
        self.assertEqual(code, 2)
        self.assertTrue(json.loads(output)['error'])
        self.commit()
        code, _ = self.run_cli('review', '--base', 'does-not-exist')
        self.assertEqual(code, 2)

    def test_summary_limits_new_candidates_and_preserves_history_in_all(self):
        (self.root / 'history.py').write_text(candidates(200))
        self.commit()
        (self.root / 'new.py').write_text(candidates(5))
        code, output = self.run_cli('review')
        self.assertEqual(code, 0)
        self.assertEqual(output.count('  fact:'), 3)
        self.assertIn('2 more', output)
        self.assertIn('persisting: 200', output)
        self.assertNotIn('history.py:', output)
        self.assertEqual((code, output), self.run_cli('review'))
        code, full = self.run_cli('review', '--all')
        self.assertEqual(code, 0)
        self.assertIn('history.py:', full)
        self.assertEqual(full.count('  fact:'), 205)
        _, raw = self.run_cli('review', '--format', 'json')
        self.assertEqual(len(json.loads(raw)['delta']['introduced']), 5)
        self.assertEqual(self.run_cli('review', '--all', '--format', 'json')[1], raw)

    def test_failure_diagnostics_and_threshold_not_limited(self):
        (self.root / 'base.py').write_text('value=1\n')
        self.commit()
        (self.root / 'new.py').write_text(candidates(5))
        self.assertEqual(self.run_cli('review', '--fail-on', 'medium')[0], 1)
        (self.root / 'broken.py').write_text('def broken(:\n')
        code, output = self.run_cli('review', '--fail-on', 'medium')
        self.assertEqual(code, 2)
        self.assertIn('broken.py', output)
        self.assertIn('Incomplete', output)

    def test_unknown_identity_is_visible_in_summary(self):
        source = 'def f():\n    try: a()\n    except OSError: return 0\n    try: a()\n    except OSError: return 0\n'
        (self.root / 'base.py').write_text(source)
        self.commit()
        code, output = self.run_cli('review')
        self.assertEqual(code, 0)
        self.assertIn('unverified: 4', output)
        self.assertIn('uncertain', output)
        self.assertIn('--all', output)
