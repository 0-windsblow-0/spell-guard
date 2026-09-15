"""Synthetic, self-cleaning demo using the installed CLI and local hook adapter."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from spellguard.registry import parse_registry
REGISTRY = {
    'schema_version': 1,
    'rules': [{
        'id': 'TEMP-001', 'classification': 'temporary', 'lifecycle': 'ACTIVE',
        'reason': 'Synthetic demo: legacy adapter is temporary and must stay isolated.',
        'desired_state': 'Synthetic demo: remove adapter after migration.',
        'protected_symbol': {'path': 'src/demo/legacy.py', 'symbol': 'adapt', 'source_root': 'src'},
        'window': 'no_external_callers', 'resolution_reason': None,
    }],
}


def run(command, repo, expected=0, payload=None):
    result = subprocess.run(command, cwd=repo, input=payload, text=True,
                            capture_output=True, timeout=20)
    if result.returncode != expected:
        raise RuntimeError(f'Expected exit {expected}, got {result.returncode}: {result.stderr}')
    return result.stdout


def main():
    print('Spellguard: synthetic demo, not a real business rule.')
    print('Uses a disposable Git repository. No Agent hook is installed; example source is not executed.')
    with tempfile.TemporaryDirectory(prefix='spellguard-demo-') as folder:
        base = Path(folder).resolve()
        repo, state = base / 'repo', base / 'state'
        (repo / 'src/demo').mkdir(parents=True)
        (repo / '.spellguard').mkdir()
        (repo / 'src/demo/__init__.py').write_text('')
        (repo / 'src/demo/legacy.py').write_text('def adapt():\n    return 0\n')
        raw = json.dumps(REGISTRY).encode()
        (repo / '.spellguard/rules.json').write_bytes(raw)
        digest = parse_registry(raw).digest  # Synthetic intent pinned once, never adopted on each check.
        git = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
               '-c', 'user.name=Spellguard demo', '-c', 'user.email=demo@example.invalid']
        run(git + ['init', '-q'], repo)
        run(git + ['add', '.'], repo)
        run(git + ['commit', '-qm', 'Synthetic baseline'], repo)

        def check(expected, status):
            text = run([sys.executable, '-m', 'spellguard', 'check',
                        '--registry-sha256', digest, '--format', 'json'], repo, expected)
            report = json.loads(text)
            if not report['complete'] or report['results'][0]['status'] != status:
                raise RuntimeError(f'Unexpected check: {report}')

        def hook(event):
            text = run([sys.executable, '-m', 'spellguard', 'hook', '--host', 'codex', '--repo', str(repo),
                        '--registry-sha256', digest, '--state-dir', str(state)], repo,
                       payload=json.dumps({'hook_event_name': event, 'cwd': str(repo),
                                           'session_id': 'synthetic', 'turn_id': 'demo'}))
            return json.loads(text)

        check(0, 'OPEN')
        if hook('Stop') != {}:
            raise RuntimeError('A normal check should be quiet')
        print('\n1. Isolated function -> OPEN. No notification.')

        caller = repo / 'src/demo/feature.py'
        caller.write_text('from demo.legacy import adapt\n\ndef feature():\n    return adapt()\n')
        check(1, 'VIOLATED')
        message = hook('Stop').get('systemMessage', '')
        if 'TEMP-001' not in message or 'feature.py:4' not in message:
            raise RuntimeError('Missing concrete violation evidence')
        print('\n2. New external caller -> VIOLATED.\n' + message)

        hook('UserPromptSubmit')
        if hook('Stop') != {}:
            raise RuntimeError('Unchanged evidence should not notify again')
        check(1, 'VIOLATED')
        print('\n3. Same evidence next turn -> no repeated notification; still VIOLATED.')

        caller.unlink()
        check(0, 'OPEN')
        if hook('Stop') != {}:
            raise RuntimeError('Recovered check should be quiet')
        print('\n4. External caller removed -> OPEN. The constraint remains active.')
    print('\nDemo passed. Temporary repository and state removed; Agent configuration unchanged.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
