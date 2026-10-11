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


def verified_code(relative):
    """Check the entire source tree before returning either code object."""
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
    return normalize_code(reference), candidate


def classes(relative, class_name, globals_, bases=()):
    """Instantiate only one statically matched class with supplied dependencies."""
    unpacker = Path(os.environ['SFML_UNPACKER_DIR'])
    sys.path.insert(0, str(unpacker / 'oneshot'))
    from bytecode_fidelity import units
    result = []
    for module in verified_code(relative):
        code = next(unit for path, unit in units(module) if path == f'<module>/{class_name}#1')
        namespace = dict(globals_, __name__='pure_recovery_fixture', __builtins__=builtins.__dict__)
        result.append(builtins.__build_class__(types.FunctionType(code, namespace), class_name, *bases))
    return tuple(result)


def functions(relative, names, globals_, *, defaults=None, kwdefaults=None):
    """Instantiate selected pure functions; share their controlled dependencies.

    The containing module is never executed. Tests provide the known default
    values normally attached by the module's MAKE_FUNCTION instructions.
    """
    result = []
    for module in verified_code(relative):
        namespace = dict(globals_, __name__='pure_recovery_fixture', __builtins__=builtins.__dict__)
        selected = {}
        for name in names:
            matches = [value for value in module.co_consts
                       if isinstance(value, types.CodeType) and value.co_name == name]
            if len(matches) != 1:
                raise AssertionError(f'{relative}: expected one top-level {name}')
            function = types.FunctionType(matches[0], namespace, name, (defaults or {}).get(name))
            function.__kwdefaults__ = dict((kwdefaults or {}).get(name, {}))
            namespace[name] = selected[name] = function
        result.append(selected)
    return tuple(result)
