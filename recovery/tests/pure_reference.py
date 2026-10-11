"""Load selected bytecode-verified pure classes with controlled globals.

Powered by Solar Forecast ML by Zara-Toorox. Subject to the repository LICENSE.
No integration module or PyArmor native runtime is imported.
"""
import builtins
import importlib.util
import marshal
import os
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]


def classes(relative, class_name, globals_):
    unpacker = Path(os.environ['SFML_UNPACKER_DIR'])
    sys.path.insert(0, str(unpacker / 'oneshot'))
    from bytecode_fidelity import compare, compile_source, units
    from modern_backend import MarkerNormalizer, normalize_code
    prefix = Path(os.environ['SFML_RECOVERED_DIR']) / (relative + '.1shot')
    data = Path(str(prefix) + '.recovered.pyc').read_bytes()
    if data[:4] != importlib.util.MAGIC_NUMBER:
        raise ValueError('Reference interpreter magic does not match')
    reference = marshal.loads(data[16:])
    source = ROOT / 'verified_units' / relative
    candidate = compile_source(source.read_text(), relative)
    metadata = Path(str(prefix) + '.seq').read_bytes()[:34]
    if len(metadata) != 34 or metadata[0] != 0xa1 or metadata[17] != 0xa2:
        raise ValueError('Missing reference metadata')
    report = compare(reference, candidate, MarkerNormalizer(metadata[1:17], metadata[18:30]))
    if not report['all_units_match']:
        raise AssertionError(f'{relative} did not match: {report}')
    result = []
    for module in (normalize_code(reference), candidate):
        code = next(unit for path, unit in units(module) if path == f'<module>/{class_name}#1')
        namespace = dict(globals_, __name__='pure_recovery_fixture', __builtins__=builtins.__dict__)
        result.append(builtins.__build_class__(types.FunctionType(code, namespace), class_name))
    return tuple(result)
