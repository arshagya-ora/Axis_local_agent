"""Desktop application entry point: `QGuiApplication`/QML engine creation,
controller registration, startup error handling, and shutdown.

Supports a deterministic `--smoke-test` mode (see `main()`) that loads
configuration, creates the Qt application, loads the QML root, confirms the
controller was registered, and exits without ever connecting to a real
provider, Temporal server, browser, or Chrome.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

AGENT_DIR = Path(__file__).resolve().parent.parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

QML_DIR = Path(__file__).resolve().parent / "qml"
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
# .ico first: it's a native Windows icon format with sizes baked in and
# needs no Qt image plugin; the .svg is kept as a fallback and as the
# source asset a real brand mark would eventually replace.
ICON_PATH = next((p for p in (ASSETS_DIR / "axis_icon.ico", ASSETS_DIR / "axis_icon.svg") if p.exists()), None)


class DesktopStartupError(RuntimeError):
    """A controlled, traceback-free startup failure."""


def _configure_desktop_logging() -> None:
    """Nothing configured Python logging for the desktop process before —
    every `logging.getLogger(...)` call in `axis.desktop.controller`/
    `axis.desktop.backend` (and anything else in-process) was silently
    dropped. INFO here shows every controller command and backend call;
    `axis.desktop.backend`'s own logger additionally uses `.exception(...)`
    so a real traceback prints for any failure, right in this terminal —
    the QML UI still only ever shows the bounded, safe error message."""
    if logging.getLogger().handlers:
        return  # already configured (e.g. by a test or the embedding process)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


def _load_config():
    from axis.config import AxisConfigError, load_axis_config

    try:
        config = load_axis_config()
    except AxisConfigError as exc:
        raise DesktopStartupError(str(exc)) from exc
    if not config.phase4.desktop.enabled:
        raise DesktopStartupError("The AXIS desktop application is disabled (phase4.desktop.enabled: false).")
    return config


def _show_native_error(message: str) -> None:
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, "AXIS Desktop", message)
        del app
    except Exception:  # noqa: BLE001 - the error dialog itself must never crash startup
        print(f"AXIS Desktop startup error: {message}", file=sys.stderr)


def run(argv: Optional[list] = None, *, smoke_test: bool = False) -> int:
    argv = list(argv if argv is not None else sys.argv)
    smoke_test = smoke_test or "--smoke-test" in argv
    if "--smoke-test" in argv:
        argv = [a for a in argv if a != "--smoke-test"]

    if not smoke_test:
        _configure_desktop_logging()

    try:
        config = _load_config()
    except DesktopStartupError as exc:
        _show_native_error(f"AXIS could not start: {exc}")
        return 1

    if not smoke_test:
        temporal = config.phase3.temporal
        print(
            f"[axis.desktop] starting - target={temporal.target} namespace={temporal.namespace} "
            f"task_queue={temporal.task_queue}"
        )
        print("[axis.desktop] every controller command and backend call will be logged below.")

    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QGuiApplication, QIcon
    from PySide6.QtQml import QQmlApplicationEngine

    from axis.desktop.backend import BackgroundLoop, TemporalDesktopBackend
    from axis.desktop.controller import AxisDesktopController

    QGuiApplication.setApplicationName("AXIS")
    QGuiApplication.setOrganizationName("AXIS")
    QGuiApplication.setOrganizationDomain("axis.local")
    app = QGuiApplication.instance() or QGuiApplication(argv)
    if ICON_PATH is not None:
        app.setWindowIcon(QIcon(str(ICON_PATH)))

    background_loop: Optional[BackgroundLoop] = None
    controller: Optional[AxisDesktopController] = None
    engine: Optional[QQmlApplicationEngine] = None
    exit_code = 0
    try:
        if smoke_test:
            controller, background_loop = _build_smoke_controller(config)
        else:
            from axis.desktop.sessions import SessionStore, default_store_path

            background_loop = BackgroundLoop()
            backend = TemporalDesktopBackend(config)
            store_path = default_store_path()
            if store_path is not None:
                print(f"[axis.desktop] chat sessions persist at {store_path}")
            controller = AxisDesktopController(
                backend, config, background_loop, session_store=SessionStore(store_path),
            )

        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("axisController", controller)
        engine.addImportPath(str(QML_DIR))
        engine.load(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")))
        if not engine.rootObjects():
            _show_native_error("AXIS could not load its interface.")
            return 1

        if smoke_test:
            exit_code = 0
        else:
            exit_code = app.exec()
    finally:
        if controller is not None:
            controller.shutdown()
        if background_loop is not None:
            background_loop.stop()
        if engine is not None:
            del engine

    return exit_code


def _build_smoke_controller(config):
    """A controller wired to an inert backend and a loop that is never
    driven — smoke mode confirms the QML root and controller registration
    load correctly without touching Temporal, the provider, or the browser."""
    from axis.desktop.backend import BackgroundLoop
    from axis.desktop.controller import AxisDesktopController

    class _InertBackend:
        async def start_job(self, task_summary: str) -> str:
            raise RuntimeError("unused in smoke mode")

        async def get_job_state(self, job_id: str):
            raise RuntimeError("unused in smoke mode")

        async def pause_job(self, job_id: str) -> None: ...
        async def resume_job(self, job_id: str) -> None: ...
        async def cancel_job(self, job_id: str, reason) -> None: ...
        async def approve_current(self, job_id: str) -> None: ...
        async def deny_current(self, job_id: str) -> None: ...
        async def submit_user_input(self, job_id: str, text: str) -> None: ...
        async def rebind_browser(self, job_id: str, reason) -> None: ...
        async def get_rebind_candidates(self, job_id: str):
            return []

        async def select_rebind_candidate(self, job_id: str, candidate_id: str) -> None: ...
        async def close(self) -> None: ...

    loop = BackgroundLoop()
    return AxisDesktopController(_InertBackend(), config, loop), loop


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
