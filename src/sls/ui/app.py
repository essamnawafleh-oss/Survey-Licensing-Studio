"""Desktop application bootstrap."""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from ..core.i18n import is_rtl, set_language, tr
from ..core.settings import Settings, load_settings_checked
from ..persistence.workspace import Workspace
from .theme import apply_theme


def create_app(argv: list[str] | None = None) -> QApplication:
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Survey & Licensing Studio")
    app.setOrganizationName("SurveyLicensingStudio")
    apply_theme(app)
    return app


def run(settings: Settings | None = None) -> int:
    problems: list[str] = []
    if settings is None:
        settings, problems = load_settings_checked()
    set_language(settings.language)
    app = create_app()
    if problems:
        QMessageBox.warning(None, tr("app.title"), tr("app.settings_problems", problems="\n".join(problems)))
    app.setLayoutDirection(Qt.RightToLeft if is_rtl() else Qt.LeftToRight)
    try:
        ws = Workspace(settings)
    except Exception as exc:  # noqa: BLE001 - startup failure must be visible to the user
        QMessageBox.critical(None, tr("app.title"), tr("app.open_failed", path=str(settings.data_path), error=str(exc)))
        return 1
    if settings.access_control_enabled:
        from .dialogs.admin_dialogs import LoginDialog

        if ws.users.count() == 0:
            QMessageBox.warning(None, tr("app.title"), tr("login.no_users"))
        else:
            dlg = LoginDialog(ws.users)
            if not dlg.exec() or dlg.user is None:
                ws.close()
                return 0
            ws.user = dlg.user
    from .main_window import MainWindow

    win = MainWindow(ws)
    win.show()
    return app.exec()
