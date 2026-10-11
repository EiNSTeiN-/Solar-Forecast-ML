"""Measure complete recovery coverage; never execute the recovered integration.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
from __future__ import annotations

import argparse
from collections import Counter
import dis
import hashlib
import importlib.util
import json
import marshal
import os
from pathlib import Path
import sys
import types
import warnings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--stage', default='recovered')
    parser.add_argument('--unpacker', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--verified-source', type=Path,
                        help='Prefer manually reconstructed modules at their original relative paths')
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 14):
        parser.error('Requires CPython 3.14')
    os.umask(0o077)
    sys.path.insert(0, str(args.unpacker.resolve() / 'oneshot'))
    from bytecode_fidelity import compare, compile_source, units
    from modern_backend import MarkerNormalizer
    work = args.work_dir.resolve()
    manifest = json.loads((work / 'input-manifest.json').read_text())
    directory = work / args.stage
    entries, reasons = [], Counter()
    for item in manifest['files']:
        relative = item['path']
        prefix = directory / (relative + '.1shot')
        entry = {'path': relative, 'scope': item.get('scope', 'main'),
                 'bytecode_verified': False, 'source_compiles': False,
                 'all_units_match': False, 'matched_units': 0, 'reference_units': 0}
        entry['input_unchanged'] = hashlib.sha256((args.source / relative).read_bytes()).hexdigest() == item['sha256']
        try:
            data = Path(str(prefix) + '.recovered.pyc').read_bytes()
            if data[:4] != importlib.util.MAGIC_NUMBER:
                raise ValueError('Mismatched interpreter magic')
            reference = marshal.loads(data[16:])
            if not isinstance(reference, types.CodeType):
                raise ValueError('Export did not contain a code object')
            for _, unit in units(reference):
                tuple(dis.get_instructions(unit))
                tuple(dis.Bytecode(unit).exception_entries)
                entry['reference_units'] += 1
            entry['bytecode_verified'] = True
            override = args.verified_source / relative if args.verified_source else None
            if override is not None and override.is_file():
                source = override.read_text()
                entry['source_origin'] = 'manual_reconstruction'
            else:
                generation = json.loads(Path(str(prefix) + '.recovery.json').read_text())
                if not generation.get('syntax_valid'):
                    raise ValueError('Source generation failed: ' + generation.get('error', 'see structural report'))
                source = Path(str(prefix) + '.cdc.py').read_text()
                entry['source_origin'] = 'decompiler_candidate'
            if not source.strip():
                raise ValueError('An empty native placeholder is not recovered source')
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', SyntaxWarning)
                candidate = compile_source(source, relative)
            entry['source_compiles'] = True
            metadata = Path(str(prefix) + '.seq').read_bytes()[:34]
            if len(metadata) != 34 or metadata[0] != 0xa1 or metadata[17] != 0xa2:
                raise ValueError('Missing sequence metadata')
            report = compare(reference, candidate, MarkerNormalizer(metadata[1:17], metadata[18:30]))
            Path(str(prefix) + '.fidelity.json').write_text(json.dumps(report, indent=2) + '\n')
            for unit in report['entries']:
                reasons[unit['reason']] += 1
            entry.update({key: value for key, value in report.items() if key not in ('entries', 'behavioral_equivalence_verified')})
        except (OSError, ValueError, TypeError, IndexError, SyntaxError, EOFError) as error:
            entry['error'] = f'{type(error).__name__}: {error}'
        entries.append(entry)
    summary = {'protected_modules': len(entries), 'bytecode_verified_modules': sum(entry['bytecode_verified'] for entry in entries),
               'source_compiles_modules': sum(entry['source_compiles'] for entry in entries),
               'all_units_match_modules': sum(entry['all_units_match'] for entry in entries),
               'reference_units': sum(entry['reference_units'] for entry in entries),
               'matched_units': sum(entry['matched_units'] for entry in entries),
               'input_files_unchanged': all(entry['input_unchanged'] for entry in entries),
               'vendor_modules_imported': 0, 'behavioral_equivalence_verified': False,
               'unit_results': dict(reasons), 'scopes': dict(Counter(entry['scope'] for entry in entries))}
    report = {'summary': summary, 'entries': entries}
    (work / (args.stage + '-fidelity.json')).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(summary, indent=2))
    return 0 if summary['all_units_match_modules'] == len(entries) else 1


if __name__ == '__main__':
    raise SystemExit(main())
