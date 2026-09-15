"""Check local Markdown links and active-document anchors, without network access."""
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r'\[[^\]\n]*\]\(([^)\n]+)\)')


def prose(text):
    return re.sub(r'^```[^\n]*\n.*?^```\s*$', '', text, flags=re.M | re.S)


def anchors(text):
    found = set(re.findall(r'<a\s+(?:id|name)=["\']([^"\']+)["\']', text))
    counts = {}
    for title in re.findall(r'^#{1,6}\s+(.+?)\s*#*$', prose(text), re.M):
        slug = ''.join(c for c in title.lower() if c in '-_ ' or unicodedata.category(c)[0] in 'LN').replace(' ', '-')
        number = counts.get(slug, 0)
        counts[slug] = number + 1
        found.add(slug + (f'-{number}' if number else ''))
    return found


def check(root):
    root = root.resolve()
    files = [root / 'README.md', *sorted(root.glob('README.*.md')),
             *([root / 'AGENTS.md'] if (root / 'AGENTS.md').exists() else []),
             *sorted((root / 'docs').rglob('*.md'))]
    errors = []
    links = 0
    for path in files:
        text = path.read_text()
        if len(re.findall(r'^```', text, re.M)) % 2:
            errors.append(f'{path.relative_to(root)}: unbalanced code fences')
        for match in LINK.finditer(prose(text)):
            raw = match.group(1).strip()
            target = raw[1:raw.index('>')] if raw.startswith('<') and '>' in raw else raw.split(' "', 1)[0]
            url = urlsplit(target)
            if url.scheme or url.netloc:
                continue
            # Historical absolute paths refer to the original local experiment.
            if target.startswith('/') and 'archive' in path.relative_to(root).parts:
                continue
            resolved = (root / unquote(url.path).lstrip('/') if url.path.startswith('/') else path.parent / unquote(url.path)) if url.path else path
            resolved = resolved.resolve()
            links += 1
            if not resolved.is_relative_to(root.resolve()):
                errors.append(f'{path.relative_to(root)}: link escapes repository {target}')
                continue
            if not resolved.exists():
                errors.append(f'{path.relative_to(root)}: missing {target}')
            elif url.fragment and resolved.suffix == '.md' and 'archive' not in resolved.relative_to(root).parts:
                if unquote(url.fragment) not in anchors(resolved.read_text()):
                    errors.append(f'{path.relative_to(root)}: missing anchor {target}')
    return files, links, errors


if __name__ == '__main__':
    files, links, errors = check(ROOT)
    for error in errors:
        print(error, file=sys.stderr)
    print(f'{len(files)} Markdown files, {links} local links, active anchors and fences: {"FAIL" if errors else "PASS"}')
    raise SystemExit(bool(errors))
