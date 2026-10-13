"""Measure complete recovery coverage; never execute the recovered integration.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
"""
from __future__ import annotations

import argparse
import ast
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


def named_source_unit(tree, path):
    """Find a named definition by the verifier's explicit occurrence path."""
    def definitions(scope):
        # Compound statement suites share their enclosing code scope.
        # A nested definition establishes the next scope; do not descend
        # through it while resolving a sibling occurrence.
        for field, value in ast.iter_fields(scope):
            children = enumerate(value) if isinstance(value, list) else [(None, value)]
            for index, child in children:
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield scope, index, child
                elif isinstance(child, ast.AST) and not isinstance(child, (ast.Lambda, ast.GeneratorExp)):
                    yield from definitions(child)

    parts = path.split('/')
    if parts.pop(0) != '<module>' or not parts:
        raise ValueError('An isolated override needs a named source-unit path')
    owner = tree
    for part in parts:
        name, count = part.rsplit('#', 1)
        occurrence = int(count)
        if occurrence < 1:
            raise ValueError('Source-unit occurrences start at one')
        matches = [(parent, index, value) for parent, index, value in definitions(owner)
                   if value.name == name]
        parent, index, owner = matches[occurrence - 1]
    if not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise ValueError('Only function-body overrides are supported')
    return parent, index, owner


def render_retained_candidate(tree, reference, decoder, formatter=None):
    """Retain verified return layout after inserting existing owner bodies.

    AST rendering alone can alter CPython's shared-return optimization. A
    source backend that supports verified layout recovery must run it after
    body insertion too. Older backends keep their existing dense rendering;
    the independent complete-candidate audit still decides equivalence.
    """
    source = ast.unparse(ast.fix_missing_locations(tree)) + '\n'
    if formatter is None:
        return source, []
    return formatter(source, reference, decoder)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--stage', default='recovered')
    parser.add_argument('--unpacker', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--verified-source', type=Path,
                        help='Prefer manually reconstructed modules at their original relative paths')
    parser.add_argument('--verified-functions', type=Path,
                        help='Manifest of separately verified source units to retain in incomplete candidates')
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 14):
        parser.error('Requires CPython 3.14')
    os.umask(0o077)
    sys.path.insert(0, str(args.unpacker.resolve() / 'oneshot'))
    from bytecode_fidelity import compare, compile_source, units
    from modern_backend import MarkerNormalizer, normalize_code
    import modern_backend
    work = args.work_dir.resolve()
    manifest = json.loads((work / 'input-manifest.json').read_text())
    directory = work / args.stage
    isolated_manifest = args.verified_functions
    if isolated_manifest is None and args.verified_source:
        proposed = args.verified_source.parent / 'verified-functions.json'
        if proposed.is_file():
            isolated_manifest = proposed
    isolated = json.loads(isolated_manifest.read_text())['units'] if isolated_manifest else []
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
            metadata = Path(str(prefix) + '.seq').read_bytes()[:34]
            if len(metadata) != 34 or metadata[0] != 0xa1 or metadata[17] != 0xa2:
                raise ValueError('Missing sequence metadata')
            decoder = MarkerNormalizer(metadata[1:17], metadata[18:30])
            # A verified nested function can be published independently while
            # its caller is still missing. Never insert an unused definition
            # merely to increase the corpus's matching-unit count.
            retained = [unit for unit in isolated if unit['module_path'] == relative
                        and unit.get('retain_in_candidate', True)]
            if retained and entry['source_origin'] == 'decompiler_candidate':
                tree = ast.parse(source)
                reference_units = dict(units(normalize_code(reference)))
                for unit in retained:
                    path = isolated_manifest.parent / unit['source_path']
                    data = path.read_bytes()
                    if (hashlib.sha256(data).hexdigest() != unit['reconstructed_source_sha256']
                            or item['sha256'] != unit['original_payload_sha256']):
                        raise ValueError('Isolated source/input hash does not match its manifest')
                    isolated_tree = ast.parse(data.decode('utf-8'))
                    isolated_code = compile_source(data.decode('utf-8'), str(path))
                    candidate_unit = dict(units(isolated_code))[unit['unit_path']]
                    proof = compare(reference_units[unit['unit_path']], candidate_unit, decoder)
                    if not proof['all_units_match']:
                        raise ValueError('Retained unit no longer matches recovered bytecode')
                    _, _, replacement = named_source_unit(isolated_tree, unit['unit_path'])
                    _, _, target = named_source_unit(tree, unit['unit_path'])
                    # The isolated shell proves this function's code tree, not
                    # its caller's registration contract. Retain decorators,
                    # arguments, defaults, annotations and type parameters from
                    # the enclosing candidate; replace only the proven body.
                    target.body = replacement.body
                source, layout_owners = render_retained_candidate(
                    tree, reference, decoder,
                    getattr(modern_backend, 'preserve_multiline_return_shapes', None))
                entry['source_origin'] = 'decompiler_candidate_with_verified_units'
                entry['retained_source_units'] = [unit['unit_path'] for unit in retained]
                entry['retained_source_layout_owners'] = layout_owners
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', SyntaxWarning)
                candidate = compile_source(source, relative)
            entry['source_compiles'] = True
            report = compare(reference, candidate, decoder)
            Path(str(prefix) + '.fidelity.json').write_text(json.dumps(report, indent=2) + '\n')
            for unit in report['entries']:
                reasons[unit['reason']] += 1
            entry.update({key: value for key, value in report.items() if key not in ('entries', 'behavioral_equivalence_verified')})
        except (OSError, ValueError, TypeError, IndexError, KeyError, SyntaxError, EOFError) as error:
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
