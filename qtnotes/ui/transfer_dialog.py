"""Перенос заметок между компьютерами по короткому коду.

Один диалог на обе роли: на старом ПК он показывает код, на новом — принимает его.
Вся работа в SyncManager, здесь только экраны и тексты.
"""

from __future__ import annotations

from PySide6.QtCore import QRegularExpression, Qt, QTimer
from PySide6.QtGui import QFont, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..sync import code_pairing as cp

_REASON_TEXT = {
    cp.REASON_CODE: "Неверный код. Проверьте цифры на другом компьютере.",
    cp.REASON_BUSY: "К другому компьютеру уже подключаются. Создайте на нём новый код.",
    cp.REASON_EXPIRED: "Время действия кода истекло. Создайте новый код.",
    cp.REASON_ATTEMPTS: "Слишком много неверных попыток. Создайте новый код.",
    cp.REASON_REJECTED: "Контрольные числа не подтверждены, компьютеры не связаны.",
    cp.REASON_ABORTED: "Связь оборвалась. Создайте новый код и повторите.",
    cp.REASON_NETWORK: "Не удалось установить соединение. Проверьте сеть и повторите.",
    cp.REASON_NOT_FOUND: (
        "Другой компьютер не найден. Проверьте, что оба в одной сети, а на другом "
        "открыт экран с кодом. Если там включён файрвол, разрешите порт: "
        f"sudo ufw allow {cp.CODE_PAIR_PORT}/tcp"),
}


def _big_label(point_size: int) -> QLabel:
    label = QLabel()
    font = QFont()
    font.setPointSize(point_size)
    font.setBold(True)
    label.setFont(font)
    label.setAlignment(Qt.AlignCenter)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label


def _hint(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setObjectName("EmptyState")
    return label


class TransferDialog(QDialog):
    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self._mgr = manager
        self._receiving = False
        self.setWindowTitle("Перенос заметок между компьютерами")
        self.setMinimumWidth(480)

        self._stack = QStackedWidget()
        self._choose_page = self._build_choose()
        self._share_page = self._build_share()
        self._enter_page = self._build_enter()
        self._confirm_page = self._build_confirm()
        self._result_page = self._build_result()
        for page in (self._choose_page, self._share_page, self._enter_page,
                     self._confirm_page, self._result_page):
            self._stack.addWidget(page)

        root = QVBoxLayout(self)
        root.addWidget(self._stack)

        # пересчёт заметок откладываем: изменения приходят пачками
        self._count_timer = QTimer(self)
        self._count_timer.setSingleShot(True)
        self._count_timer.setInterval(300)
        self._count_timer.timeout.connect(self._refresh_counts)

        manager.codeOfferReady.connect(self._on_offer_ready)
        manager.codeSasReady.connect(self._on_sas)
        manager.codePairFinished.connect(self._on_finished)
        manager.remoteChanged.connect(self._count_timer.start)

    # --- экраны ---

    def _build_choose(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(_hint(
            "Откройте этот экран на обоих компьютерах. Они должны быть в одной сети. "
            "Синхронизация между ними включится и будет работать дальше."))
        self._new_pc_btn = QPushButton("Это новый компьютер: ввести код")
        self._new_pc_btn.setMinimumHeight(42)
        self._new_pc_btn.clicked.connect(self._show_enter)
        lay.addWidget(self._new_pc_btn)
        self._old_pc_btn = QPushButton("Здесь уже есть заметки: показать код")
        self._old_pc_btn.setMinimumHeight(42)
        self._old_pc_btn.clicked.connect(self._start_share)
        lay.addWidget(self._old_pc_btn)
        return page

    def _build_share(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(_hint("Введите этот код на новом компьютере:"))
        self._code_label = _big_label(36)
        lay.addWidget(self._code_label)
        self._share_status = _hint()
        lay.addWidget(self._share_status)
        return page

    def _build_enter(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(_hint("Введите код, который показывает другой компьютер:"))
        self._code_edit = QLineEdit()
        self._code_edit.setAlignment(Qt.AlignCenter)
        self._code_edit.setPlaceholderText("000 000")
        font = self._code_edit.font()
        font.setPointSize(24)
        self._code_edit.setFont(font)
        self._code_edit.setValidator(QRegularExpressionValidator(
            QRegularExpression(r"[0-9 ]{0,7}"), self._code_edit))
        self._code_edit.textChanged.connect(self._update_connect_btn)
        self._code_edit.returnPressed.connect(self._start_join)
        lay.addWidget(self._code_edit)
        self._connect_btn = QPushButton("Подключить")
        self._connect_btn.setMinimumHeight(42)
        self._connect_btn.setEnabled(False)
        self._connect_btn.clicked.connect(self._start_join)
        lay.addWidget(self._connect_btn)
        self._enter_status = _hint()
        lay.addWidget(self._enter_status)
        return page

    def _build_confirm(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        self._confirm_hint = _hint()
        lay.addWidget(self._confirm_hint)
        self._sas_label = _big_label(36)
        lay.addWidget(self._sas_label)
        row = QHBoxLayout()
        self._match_btn = QPushButton("Числа совпадают")
        self._match_btn.setMinimumHeight(42)
        self._match_btn.clicked.connect(lambda: self._answer_sas(True))
        row.addWidget(self._match_btn)
        self._mismatch_btn = QPushButton("Числа разные")
        self._mismatch_btn.setMinimumHeight(42)
        self._mismatch_btn.clicked.connect(lambda: self._answer_sas(False))
        row.addWidget(self._mismatch_btn)
        lay.addLayout(row)
        self._confirm_status = _hint()
        lay.addWidget(self._confirm_status)
        return page

    def _build_result(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        self._result_label = _hint()
        self._result_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self._result_label)
        self._count_label = _hint()
        lay.addWidget(self._count_label)
        row = QHBoxLayout()
        self._retry_btn = QPushButton("Повторить")
        self._retry_btn.clicked.connect(self._retry)
        row.addWidget(self._retry_btn)
        self._close_btn = QPushButton("Закрыть")
        self._close_btn.clicked.connect(self.accept)
        row.addWidget(self._close_btn)
        lay.addLayout(row)
        return page

    # --- действия ---

    def _show_enter(self) -> None:
        self._receiving = True
        self._enter_status.clear()
        self._code_edit.setEnabled(True)
        self._update_connect_btn()
        self._stack.setCurrentWidget(self._enter_page)
        self._code_edit.setFocus()

    def _with_wait_cursor(self, action):
        """Первое включение синхронизации занимает секунду: показать занятость."""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            return action()
        finally:
            QApplication.restoreOverrideCursor()

    def _start_share(self) -> None:
        self._receiving = False
        code = self._with_wait_cursor(self._mgr.start_code_offer)
        if code is None:
            self._show_result(False, cp.REASON_NETWORK)
            return
        self._code_label.setText(cp.format_digits(code))
        self._share_status.setText("Готовлю подключение…")
        self._stack.setCurrentWidget(self._share_page)

    def _update_connect_btn(self) -> None:
        self._connect_btn.setEnabled(
            self._code_edit.isEnabled()
            and len(cp.normalize_code(self._code_edit.text())) == 6)

    def _start_join(self) -> None:
        code = cp.normalize_code(self._code_edit.text())
        if len(code) != 6 or not self._code_edit.isEnabled():
            return
        self._code_edit.setEnabled(False)
        self._connect_btn.setEnabled(False)
        self._enter_status.setText("Ищу другой компьютер…")
        if not self._with_wait_cursor(lambda: self._mgr.join_code_offer(code)):
            self._show_result(False, cp.REASON_NETWORK)

    def _answer_sas(self, matches: bool) -> None:
        self._match_btn.setEnabled(False)
        self._mismatch_btn.setEnabled(False)
        if matches:
            self._confirm_status.setText("Жду подтверждения на другом компьютере…")
        self._mgr.answer_code_sas(matches)

    def _retry(self) -> None:
        self._stack.setCurrentWidget(self._choose_page)

    # --- сигналы менеджера ---

    def _on_offer_ready(self) -> None:
        self._share_status.setText(
            f"Код действует {int(cp.CODE_TTL // 60)} минуты. Не закрывайте это окно.")

    def _on_sas(self, sas: str, name: str) -> None:
        other = f"«{name}»" if name else "другим компьютером"
        self._confirm_hint.setText(
            f"Соединение с {other}. Сверьте число на обоих экранах: "
            "если оно разное, кто-то мог вклиниться в соединение.")
        self._sas_label.setText(cp.format_digits(sas))
        self._confirm_status.clear()
        self._match_btn.setEnabled(True)
        self._mismatch_btn.setEnabled(True)
        self._stack.setCurrentWidget(self._confirm_page)

    def _on_finished(self, ok: bool, info: str) -> None:
        self._show_result(ok, info)

    def _show_result(self, ok: bool, info: str) -> None:
        self._retry_btn.setVisible(not ok)
        if ok:
            self._result_label.setText(
                f"Компьютер «{info}» подключён. Синхронизация работает, пока оба "
                "компьютера в одной сети.")
            self._close_btn.setDefault(True)
            if self._receiving:
                self._refresh_counts()
            else:
                self._count_label.clear()
        else:
            self._result_label.setText(_REASON_TEXT.get(info, _REASON_TEXT[cp.REASON_ABORTED]))
            self._count_label.clear()
        self._stack.setCurrentWidget(self._result_page)

    def _refresh_counts(self) -> None:
        if not self._receiving or self._stack.currentWidget() is not self._result_page:
            return
        from ..storage import vault
        folders = vault.list_folders()
        notes = sum(len(vault.list_notes(f.id)) for f in folders)
        self._count_label.setText(
            f"На этом компьютере сейчас папок: {len(folders)}, заметок: {notes}. "
            "Числа растут, пока идёт загрузка.")

    def done(self, result: int) -> None:  # noqa: A003
        self._count_timer.stop()
        self._mgr.cancel_code_pairing()
        for signal, slot in ((self._mgr.codeOfferReady, self._on_offer_ready),
                             (self._mgr.codeSasReady, self._on_sas),
                             (self._mgr.codePairFinished, self._on_finished),
                             (self._mgr.remoteChanged, self._count_timer.start)):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        super().done(result)
