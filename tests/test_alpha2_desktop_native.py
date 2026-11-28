import os

import pytest

pytest.importorskip("PyQt6")


def test_desktop_native_imports():
    import spectra_alpha2.desktop_native as native

    assert hasattr(native, "main")
    assert native.DASHBOARD_PORT == 8765


def test_desktop_native_single_instance_lock_path():
    import spectra_alpha2.desktop_native as native

    lock = native._single_instance_lock()
    assert lock is not None
    lock.unlock()