"""Regression checks for the mixed-runtime Windows 7 installation failure."""
import importlib.util
import struct
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    'verify_win7_payload', Path(__file__).parents[1] / 'packaging/verify_win7_payload.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


@pytest.fixture
def payload(tmp_path, monkeypatch):
    records = {}
    for name in checker.REQUIRED:
        path = tmp_path / name
        path.touch()
        version = (3, 8, 10, 0) if name.startswith('python') else (14, 29, 30133, 0)
        if name == 'ucrtbase.dll':
            version = (10, 0, 19041, 0)
        exports = {'PyObject_Call': 'python38.PyObject_Call'} if name == 'python3.dll' else {}
        records[path] = (0x14c, version, [], exports)
    monkeypatch.setattr(checker, 'inspect', lambda path: records[path])
    return tmp_path, records


def test_consistent_payload(payload):
    root, _ = payload
    assert checker.verify(root) == []


@pytest.mark.parametrize('name', ['python3.dll', 'ucrtbase.dll', 'VCRUNTIME140_1.dll'])
def test_rejects_64bit_runtime_even_in_subdirectory(payload, name):
    root, records = payload
    nested = root / 'pymupdf'
    nested.mkdir()
    path = nested / name
    path.touch()
    records[path] = (0x8664, (3, 12, 10, 0), [], {})
    assert any('expected x86' in e for e in checker.verify(root))


def test_rejects_wrong_python_forwarder(payload):
    root, records = payload
    records[root / 'python3.dll'] = (0x14c, (3, 8, 10, 0), [], {'PyObject_Call': 'python312.PyObject_Call'})
    assert any('forward only to python38' in e for e in checker.verify(root))


@pytest.mark.parametrize('api', ['CreateFile2', 'CopyFile2', 'GetSystemTimePreciseAsFileTime'])
def test_rejects_new_windows_functions(payload, api):
    root, records = payload
    records[root / 'msvcp140.dll'] = (0x14c, (14, 29, 30133, 0), [('kernel32.dll', api)], {})
    assert any(api in e and 'newer Windows' in e for e in checker.verify(root))


def test_rejects_unpinned_runtime(payload):
    root, records = payload
    records[root / 'msvcp140.dll'] = (0x14c, (14, 51, 36247, 0), [], {})
    assert any('expected VC142' in e for e in checker.verify(root))


def test_rejects_missing_import_in_bundled_dependency(payload):
    root, records = payload
    path = root / '_extra.pyd'
    path.touch()
    records[path] = (0x14c, None, [('msvcp140.dll', 'missing_function')], {})
    assert any('lacks imported symbol missing_function' in e for e in checker.verify(root))


def test_requires_runtime_even_when_nested_copy_exists(payload):
    root, records = payload
    (root / 'python3.dll').unlink()
    nested = root / 'pymupdf'
    nested.mkdir()
    path = nested / 'python3.dll'
    path.touch()
    records[path] = records[root / 'python3.dll']
    assert any('python3.dll: required runtime missing' in e for e in checker.verify(root))


def test_missing_runtime_dependency_cannot_fall_back_to_build_machine(payload):
    root, records = payload
    path = root / '_extra.pyd'
    path.touch()
    records[path] = (0x14c, None, [('msvcp140_atomic_wait.dll', 'some_function')], {})
    assert any('msvcp140_atomic_wait.dll is not bundled' in e for e in checker.verify(root))


def test_reads_real_pe_headers_and_import_table(tmp_path):
    # Small synthetic PE with a single KERNEL32 import; no Windows execution.
    data = bytearray(1536)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 60, 128)
    data[128:132] = b'PE\0\0'
    struct.pack_into('<HHIIIHH', data, 132, 0x14c, 1, 0, 0, 0, 224, 0x2102)
    opt = 152
    struct.pack_into('<H', data, opt, 0x10b)
    struct.pack_into('<I', data, opt + 28, 0x400000)
    struct.pack_into('<II', data, opt + 32, 4096, 512)
    struct.pack_into('<II', data, opt + 56, 8192, 512)
    struct.pack_into('<I', data, opt + 92, 16)
    struct.pack_into('<II', data, opt + 104, 4096, 40)
    sec = opt + 224
    data[sec:sec+8] = b'.rdata\0\0'
    struct.pack_into('<IIII', data, sec + 8, 1024, 4096, 1024, 512)
    struct.pack_into('<IIIII', data, 512, 4160, 0, 0, 4200, 4160)
    struct.pack_into('<II', data, 576, 4240, 0)
    data[616:629] = b'KERNEL32.dll\0'
    data[658:670] = b'CreateFile2\0'
    path = tmp_path / 'sample.dll'
    path.write_bytes(data)
    machine, _, imports, _ = checker.inspect(path)
    assert machine == 0x14c
    assert ('kernel32.dll', 'CreateFile2') in imports


def test_invalid_pe_is_reported(tmp_path):
    (tmp_path / 'broken.dll').write_bytes(b'not a PE file')
    assert any('invalid PE' in e for e in checker.verify(tmp_path))
