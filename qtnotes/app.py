"""Точка сборки приложения: QApplication, тема, главное окно."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .ui.main_window import MainWindow, apply_font
from .ui.theme import build_qss


def _install_ru_translation(app: QApplication) -> None:
    """Перевести стандартные элементы Qt (меню Вырезать/Копировать и т.п.)."""
    tr_dir = QLibraryInfo.path(QLibraryInfo.TranslationsPath)
    translator = QTranslator(app)
    if translator.load(QLocale("ru"), "qtbase", "_", tr_dir):
        app.installTranslator(translator)
        app._ru_translator = translator  # удержать ссылку


def run() -> int:
    # установить цветной emoji-шрифт в пользовательские шрифты до старта Qt
    from .ui.fonts import ensure_emoji_font
    ensure_emoji_font()

    app = QApplication(sys.argv)
    app.setApplicationName("QtNotes")
    # иконка окна для панели/дока и связка с qtnotes.desktop (та же, что у note-mobile)
    app.setDesktopFileName("qtnotes")
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "resources" / "icon.png")))
    _install_ru_translation(app)
    app.setStyleSheet(build_qss())
    apply_font(app)

    # Диагностика «ПКМ открывается медленно / не открывается»: включается только
    # переменной QTNOTES_DEBUG_CTX=1, без неё — no-op (см. qtnotes/dbg.py).
    from . import dbg
    if dbg.install(app):
        from . import config
        dbg.log("start", sync_enabled=config.get_setting("sync_enabled", False),
                font=config.font_family() or "system")

    # гейт разблокировки: если настроено локальное шифрование — спросить ПИН ДО
    # доступа к данным (индекс перестраивается из РАСШИФРОВАННЫХ заметок, нужен MK).
    if not _unlock_gate():
        return 0

    # построить поисковый индекс, если его ещё нет (первый запуск/после импорта)
    from .storage import index
    index.ensure_ready()

    window = MainWindow()
    window.show()

    # Отложенная сборка мусора блобов: подобрать осиротевшие вложения удалённых заметок
    # (в т.ч. удалённых синком). После первого отображения, чтобы не задерживать старт.
    from PySide6.QtCore import QTimer
    from .storage import vault
    QTimer.singleShot(1500, vault.gc_blobs)

    return app.exec()


def _unlock_gate() -> bool:
    """True — продолжать (разблокировано или шифрование не настроено).
    False — пользователь отказался (выход)."""
    from .crypto import unlock
    if not unlock.is_configured():
        return True
    from PySide6.QtWidgets import QDialog, QMessageBox
    try:
        backend = unlock.default_backend()
    except RuntimeError as e:
        QMessageBox.critical(None, "Локальное шифрование", str(e))
        return False
    from .ui.pin_dialog import PinUnlockDialog
    dlg = PinUnlockDialog(
        check=lambda pin: unlock.try_unlock(pin, backend),
        remaining=lambda: unlock.remaining_lockout(backend))
    return dlg.exec() == QDialog.Accepted
