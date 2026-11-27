"""Native PyQt6 desktop shell for Spectra Finance Alpha 2.0.

This module wraps the existing loopback dashboard in a native application window.
It does not add order or live-execution capabilities. Core remains independently
controlled through the existing backend.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QLockFile, Qt, QTimer, QUrl
from PyQt6.QtGui import QColor, QFont, QIcon, QPixmap
from PyQt6.QtWidgets import QApplication, QLabel, QSplashScreen, QWidget
from PyQt6.QtWebEngineWidgets import QWebEngineView

HERE = Path(__file__).resolve().parent
DASHBOARD_PORT = 8765


def _single_instance_lock() -> QLockFile:
    lock_path = Path.home() / ".spectra-alpha2-desktop.lock"
    lock = QLockFile(str(lock_path))
    if not lock.tryLock(100):
        raise RuntimeError("Spectra Finance Alpha 2.0 desktop is already running")
    return lock


def _start_dashboard_server() -> None:
    from spectra_alpha2 import desktop

    server = desktop.ThreadingHTTPServer(("127.0.0.1", DASHBOARD_PORT), desktop.DesktopHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()


def _wait_for_dashboard(timeout_seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", DASHBOARD_PORT), timeout=1):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _make_splash() -> QSplashScreen:
    pixmap = QPixmap(480, 280)
    pixmap.fill(QColor("#10151d"))
    splash = QSplashScreen(pixmap, Qt.WindowType.WindowStaysOnTopHint)
    splash.setWindowFlag(Qt.WindowType.FramelessWindowHint)
    label = QLabel("Spectra Finance\nAlpha 2.0", splash)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setStyleSheet("color: #7bd5c5; font-size: 28px; font-weight: 700;")
    label.setGeometry(0, 90, 480, 80)
    sub = QLabel("Starting paper-only desktop shell…", splash)
    sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
    sub.setStyleSheet("color: #9aaabd; font-size: 12px;")
    sub.setGeometry(0, 180, 480, 30)
    return splash


def main(argv=None) -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Spectra Finance Alpha 2.0")
    app.setApplicationDisplayName("Spectra Finance Alpha 2.0")
    app.setOrganizationName("Spectra Finance")

    lock = _single_instance_lock()

    splash = _make_splash()
    splash.show()
    app.processEvents()

    _start_dashboard_server()
    ready = _wait_for_dashboard()
    if not ready:
        splash.close()
        from PyQt6.QtWidgets import QMessageBox

        QMessageBox.critical(None, "Spectra Finance", "The local dashboard did not start.")
        return 1

    splash.showMessage("Dashboard ready", Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter, QColor("#9aaabd"))
    app.processEvents()

    window = QWidget()
    window.setWindowTitle("Spectra Finance Alpha 2.0")
    window.resize(1280, 820)
    view = QWebEngineView(window)
    view.setUrl(QUrl(f"http://127.0.0.1:{DASHBOARD_PORT}/"))
    view.setGeometry(0, 0, 1280, 820)
    window.show()

    splash.finish(window)

    try:
        return app.exec()
    finally:
        lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
