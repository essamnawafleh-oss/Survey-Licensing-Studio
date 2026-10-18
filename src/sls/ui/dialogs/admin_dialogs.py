"""Settings, local users, login and backup restore."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton, QSpinBox,
                               QVBoxLayout, QWidget)

from ...cad.dwg import dwg_status
from ...core.i18n import tr
from ...core.settings import Settings
from ...licensing.ocr import find_tesseract
from ...persistence.backup import list_backups
from ...persistence.users import ROLES, UserRepository
from .common import make_table, fill_table, ok_cancel, show_error


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle(tr("settings.title"))
        f = QFormLayout(self)
        self.data_dir = QLineEdit(str(settings.data_path))
        browse = QPushButton("…")
        browse.clicked.connect(lambda: self._dir(self.data_dir))
        row = QHBoxLayout()
        row.addWidget(self.data_dir)
        row.addWidget(browse)
        self.lang = QComboBox()
        self.lang.addItem("العربية", "ar")
        self.lang.addItem("English", "en")
        self.lang.setCurrentIndex(0 if settings.language == "ar" else 1)
        self.area_dec = QSpinBox()
        self.area_dec.setRange(0, 4)
        self.area_dec.setValue(settings.number_format.area_decimals)
        self.len_dec = QSpinBox()
        self.len_dec.setRange(0, 4)
        self.len_dec.setValue(settings.number_format.length_decimals)
        self.indic = QCheckBox(tr("settings.arabic_digits"))
        self.indic.setChecked(settings.number_format.arabic_indic_digits)
        self.access = QCheckBox(tr("settings.access_control"))
        self.access.setChecked(settings.access_control_enabled)
        self.retention = QSpinBox()
        self.retention.setRange(1, 3650)
        self.retention.setValue(settings.backup_retention)
        self.oda = QLineEdit(settings.oda_converter_path)
        self.libredwg = QLineEdit(settings.libredwg_path)
        self.oda_status = QLabel(dwg_status(settings.oda_converter_path, settings.libredwg_path))
        self.oda_status.setWordWrap(True)
        self.oda_status.setObjectName("muted")
        self.tess = QLineEdit(settings.tesseract_path)
        tess = find_tesseract(settings.tesseract_path)
        self.tess_status = QLabel(tr("settings.tesseract_found", path=tess) if tess else tr("settings.tesseract_missing"))
        self.tess_status.setObjectName("muted")
        self.sliver = QDoubleSpinBox()
        self.sliver.setDecimals(4)
        self.sliver.setRange(0.0001, 10)
        self.sliver.setValue(settings.sliver_area_tolerance_m2)
        self.sliver.setSuffix(" m²")
        f.addRow(tr("settings.data_dir"), row)
        f.addRow("", QLabel(tr("settings.restart_note")))
        f.addRow(tr("settings.language"), self.lang)
        f.addRow(tr("settings.area_decimals"), self.area_dec)
        f.addRow(tr("settings.length_decimals"), self.len_dec)
        f.addRow("", self.indic)
        f.addRow("", self.access)
        note = QLabel(tr("settings.access_note"))
        note.setWordWrap(True)
        note.setObjectName("warning")
        f.addRow(note)
        f.addRow(tr("settings.backup_retention"), self.retention)
        f.addRow(tr("settings.oda_path"), self.oda)
        f.addRow(tr("settings.libredwg_path"), self.libredwg)
        f.addRow("", self.oda_status)
        f.addRow(tr("settings.tesseract_path"), self.tess)
        f.addRow("", self.tess_status)
        f.addRow(tr("settings.sliver"), self.sliver)
        f.addRow(ok_cancel(self))

    def _dir(self, edit: QLineEdit) -> None:
        d = QFileDialog.getExistingDirectory(self, tr("settings.data_dir"), edit.text())
        if d:
            edit.setText(d)

    def apply(self) -> Settings:
        s = self.settings
        s.data_dir = self.data_dir.text().strip()
        s.language = self.lang.currentData()
        s.number_format.area_decimals = self.area_dec.value()
        s.number_format.length_decimals = self.len_dec.value()
        s.number_format.arabic_indic_digits = self.indic.isChecked()
        s.access_control_enabled = self.access.isChecked()
        s.backup_retention = self.retention.value()
        s.oda_converter_path = self.oda.text().strip()
        s.libredwg_path = self.libredwg.text().strip()
        s.tesseract_path = self.tess.text().strip()
        s.sliver_area_tolerance_m2 = self.sliver.value()
        return s


class UsersDialog(QDialog):
    def __init__(self, users: UserRepository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.users = users
        self.setWindowTitle(tr("users.title"))
        self.resize(520, 420)
        lay = QVBoxLayout(self)
        self.table = make_table([tr("users.username"), tr("users.display"), tr("users.role")])
        lay.addWidget(self.table)
        f = QFormLayout()
        self.username = QLineEdit()
        self.display = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.role = QComboBox()
        for r in ROLES:
            self.role.addItem(tr(f"users.role.{r}"), r)
        f.addRow(tr("users.username"), self.username)
        f.addRow(tr("users.display"), self.display)
        f.addRow(tr("users.password"), self.password)
        f.addRow(tr("users.role"), self.role)
        lay.addLayout(f)
        add = QPushButton(tr("users.add"))
        add.clicked.connect(self._add)
        lay.addWidget(add)
        self._refresh()

    def _refresh(self) -> None:
        fill_table(self.table, [[u.username, u.display_name, tr(f"users.role.{u.role}")] for u in self.users.list()])

    def _add(self) -> None:
        try:
            self.users.create(self.username.text(), self.password.text(), self.role.currentData(), self.display.text())
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return
        self.username.clear()
        self.password.clear()
        self.display.clear()
        self._refresh()


class LoginDialog(QDialog):
    def __init__(self, users: UserRepository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.users = users
        self.user = None
        self.setWindowTitle(tr("login.title"))
        f = QFormLayout(self)
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.msg = QLabel()
        self.msg.setObjectName("error")
        f.addRow(tr("users.username"), self.username)
        f.addRow(tr("users.password"), self.password)
        f.addRow(self.msg)
        f.addRow(ok_cancel(self))

    def accept(self) -> None:
        self.user = self.users.authenticate(self.username.text().strip(), self.password.text())
        if self.user is None:
            self.msg.setText(tr("login.failed"))
            return
        super().accept()


class RestoreDialog(QDialog):
    def __init__(self, backup_dir: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("backup.restore_title"))
        self.resize(560, 360)
        self.selected: Path | None = None
        lay = QVBoxLayout(self)
        warn = QLabel(tr("backup.restore_note"))
        warn.setWordWrap(True)
        warn.setObjectName("warning")
        lay.addWidget(warn)
        self.backups = list(reversed(list_backups(backup_dir)))
        self.list = QListWidget()
        for b in self.backups:
            self.list.addItem(b.name)
        lay.addWidget(self.list)
        other = QPushButton(tr("backup.other_file"))
        other.clicked.connect(self._other)
        lay.addWidget(other)
        lay.addWidget(ok_cancel(self))

    def _other(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("backup.other_file"), "", "SQLite (*.sqlite3 *.db)")
        if path:
            self.backups.insert(0, Path(path))
            self.list.insertItem(0, path)
            self.list.setCurrentRow(0)

    def accept(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        if QMessageBox.question(self, tr("backup.restore_title"), tr("backup.confirm", name=self.backups[row].name)) \
                != QMessageBox.Yes:
            return
        self.selected = self.backups[row]
        super().accept()
