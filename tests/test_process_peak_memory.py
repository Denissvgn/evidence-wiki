"""Native high-water memory, portable import and measurement failure checks."""

import ctypes
import subprocess
import sys
from types import SimpleNamespace

import pytest

from evidence_wiki import performance_probe as probe


def test_probe_import_does_not_require_posix_resource():
    result = subprocess.run([sys.executable, '-c',
        "import sys; sys.modules['resource']=None; import evidence_wiki.performance_probe"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_native_peak_resident_memory_is_positive():
    result = probe._process_peak_rss_bytes()
    assert type(result) is int and result > 0


@pytest.mark.parametrize(('platform', 'factor'), [('darwin', 1), ('linux', 1024)])
def test_posix_high_water_units_are_preserved(monkeypatch, platform, factor):
    monkeypatch.setattr(probe, 'sys', SimpleNamespace(platform=platform))
    monkeypatch.setitem(sys.modules, 'resource', SimpleNamespace(RUSAGE_SELF=0,
        getrusage=lambda who: SimpleNamespace(ru_maxrss=12345)))
    assert probe._process_peak_rss_bytes() == 12345 * factor


@pytest.mark.parametrize('failure', [False, True])
def test_windows_uses_native_peak_working_set_and_refuses_api_failure(monkeypatch, failure):
    expected = 2**33 + 7 if ctypes.sizeof(ctypes.c_size_t) == 8 else 12345
    handle = ctypes.c_void_p(-1).value

    def current_process():
        return handle

    def memory_info(selected, pointer, size):
        assert selected == handle
        assert size == 8 + 8 * ctypes.sizeof(ctypes.c_size_t) == ctypes.sizeof(pointer._obj)
        assert pointer._obj.cb == size
        pointer._obj.PeakWorkingSetSize = expected
        return not failure

    kernel = SimpleNamespace(GetCurrentProcess=current_process, K32GetProcessMemoryInfo=memory_info)

    def load(name, **options):
        assert name == 'kernel32' and options == {'use_last_error': True}
        return kernel

    monkeypatch.setattr(probe, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(ctypes, 'WinDLL', load, raising=False)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: 5, raising=False)
    monkeypatch.setattr(ctypes, 'WinError', lambda code: OSError(code, 'Native memory query failed'), raising=False)
    if failure:
        with pytest.raises(OSError, match='Native memory query failed'):
            probe._process_peak_rss_bytes()
    else:
        assert probe._process_peak_rss_bytes() == expected
    assert current_process.restype is ctypes.c_void_p
    assert memory_info.restype is ctypes.c_int
