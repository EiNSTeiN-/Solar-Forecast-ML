"""Inventory every protected payload without importing it.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    source, work = args.source.resolve(), args.work_dir.resolve()
    if work.is_relative_to(source):
        parser.error('Keep private recovery artifacts outside the integration source')
    target = work / 'input'
    files = []
    for path in sorted(source.rglob('*.py')):
        text = path.read_bytes()
        if b'__pyarmor__(' not in text or b'from pyarmor_runtime_' not in text:
            continue
        relative = path.relative_to(source)
        copied = target / relative
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, copied)
        scope = relative.parts[1] if relative.parts[0] == 'extra_features' else 'main'
        files.append({'path': relative.as_posix(), 'scope': scope,
                      'sha256': digest(path), 'bytes': path.stat().st_size})
    runtimes = []
    for path in sorted(source.rglob('linux_x86_64/pyarmor_runtime.so')):
        relative = path.relative_to(source)
        copied = target / relative
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, copied)
        runtimes.append({'path': relative.as_posix(), 'sha256': digest(path)})
    work.mkdir(parents=True, exist_ok=True)
    manifest = {'attribution': 'Powered by Solar Forecast ML by Zara-Toorox',
                'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                                        cwd=source, text=True).strip(),
                'scope': 'All protected payloads, including bundled companion integrations',
                'files': files, 'runtimes': runtimes}
    (work / 'input-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'protected_modules': len(files), 'runtimes': len(runtimes),
                      'scopes': dict(Counter(item['scope'] for item in files))}))


if __name__ == '__main__':
    main()
