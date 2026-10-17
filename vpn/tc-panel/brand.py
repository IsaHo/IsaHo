#!/usr/bin/env python3
"""Apply a display-only overlay to the exact, installed 3X-UI release.

Keep upstream licensing, API contracts, service names and database identity.
Never use a global replacement: config.GetName() determines the database path.
"""
import argparse
import json
from pathlib import Path
import subprocess

UPSTREAM_COMMIT = "608ef4e4010bf83e2e78328e08f77efc051ad4d7"
REPLACEMENTS = {
    "frontend/src/pages/login/LoginPage.tsx": [('>3X-UI</span>', '>TC VPN</span>')],
    "frontend/src/layouts/AppSidebar.tsx": [
        ("railCollapsed ? '3X' : '3X-UI'", "railCollapsed ? 'TC' : 'TC VPN'"),
        ('>3X-UI</span>', '>TC VPN</span>'),
    ],
    "frontend/src/hooks/usePageTitle.ts": [
        ("const title = key ? t(key) : '3X-UI';", "const title = key ? t(key) : 'TC VPN';"),
        ("const host = window.location.hostname;", "const host = 'TC VPN';"),
    ],
    "frontend/src/components/command-palette/CommandPalette.tsx": [
        ('>3x-ui Command Palette</span>', '>TC VPN Command Palette</span>'),
    ],
    "frontend/src/pages/api-docs/ApiDocsPage.tsx": [
        ("label: '3X-UI Panel API'", "label: 'TC VPN Panel API'"),
    ],
    "frontend/index.html": [('<meta charset="UTF-8" />', '<meta charset="UTF-8" />\n    <title>TC VPN</title>')],
    "frontend/login.html": [('<title>Sign in</title>', '<title>TC VPN · Sign in</title>')],
    "frontend/public/manifest.webmanifest": [
        ('"name": "3x-ui"', '"name": "TC VPN"'),
        ('"short_name": "3x-ui"', '"short_name": "TC VPN"'),
    ],
}


def apply(root: Path):
    revision = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != UPSTREAM_COMMIT:
        raise ValueError('upstream release mismatch; review overlay before upgrading')
    # Validate all anchors before writing any file. Refuse mixed/partial overlays.
    changes = {}
    for relative, replacements in REPLACEMENTS.items():
        path = root / relative
        content = path.read_text()
        for before, after in replacements:
            if content.count(before) != 1:
                raise ValueError(f'expected exactly one branding anchor in {relative}')
            content = content.replace(before, after)
        changes[path] = content
    for path, content in changes.items():
        path.write_text(content)
    return {'brand': 'TC VPN', 'upstream': revision, 'files': len(changes), 'backend_changes': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    print(json.dumps(apply(parser.parse_args().source)))
