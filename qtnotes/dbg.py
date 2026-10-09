"""Диагностика «ПКМ открывается медленно / не открывается».

Включается переменной окружения `QTNOTES_DEBUG_CTX=1`; путь к файлу — `QTNOTES_DEBUG_CTX_LOG`
(по умолчанию `/tmp/qtnotes-ctx.log`). Пока флаг не задан, ничего не пишется и поведение
приложения не меняется — все функции выходят сразу.

Зачем нужен каждый вид записи:

| Запись | Что доказывает |
|---|---|
| `press` / `wait_ms` в `ctx` | сколько событие ПКМ прождало в очереди: большое `wait_ms` — главный поток был занят |
| `ctx_show` (`build_ms`, `wait_ms`) | сколько прошло от создания меню до его показа |
| `ctx_done` | что выбрано в меню (видно, дошло ли дело до «Изменить») |
| `edit_note` | вход в правку и её длительность; `phase=miss` — пузырь не найден и правка молча не включилась |
| `rebuild` / `clear_bubbles` / `render_chunk` | пересборка ленты: сколько заняла и сколько пузырей затронула |
| `modal` | модальное окно открыто/закрыто: пока оно открыто, клики по ленте не доходят |
| `stall` | главный поток не отвечал дольше порога — прямой ответ, «виноват ли поток» |
"""

from __future__ import annotations

import os
import threading
import time

_ENV = "QTNOTES_DEBUG_CTX"
_ENV_LOG = "QTNOTES_DEBUG_CTX_LOG"
_DEFAULT_LOG = "/tmp/qtnotes-ctx.log"

_enabled: bool | None = None
_fh = None
_t0 = time.perf_counter()
_lock = threading.Lock()
_last_press: tuple[float, str] | None = None
_last_press_pos: tuple[float | None, float | None] = (None, None)
_last_tick: float | None = None

_BUTTONS = {1: "left", 2: "right", 4: "middle"}


def enabled() -> bool:
    """Включена ли диагностика (читается один раз за процесс)."""
    global _enabled
    if _enabled is None:
        _enabled = os.environ.get(_ENV, "") not in ("", "0", "false", "False")
    return _enabled


def log_path() -> str:
    return os.environ.get(_ENV_LOG) or _DEFAULT_LOG


def fmt_ms(value: float | None) -> str:
    """Мс одной строкой с одним знаком; None (нечего мерить) — прочерк."""
    return "-" if value is None else f"{value:.1f}"


def log(event: str, **fields) -> None:
    """Одна строка лога. Без включённого флага — no-op."""
    if not enabled():
        return
    global _fh
    now = time.time()
    wall = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    thread = threading.current_thread().name
    line = (f"{wall} | +{time.perf_counter() - _t0:8.3f} | {thread:>12} | "
            f"{event:<12} | {extra}\n")
    with _lock:
        try:
            if _fh is None:
                _fh = open(log_path(), "a", buffering=1, encoding="utf-8")
            _fh.write(line)
        except OSError:
            pass  # диагностика не должна ломать приложение


class span:
    """Замер участка: пишет `<event> phase=start` и `phase=done dur_ms=...`."""

    def __init__(self, event: str, **fields):
        self.event = event
        self.fields = fields
        self.t0 = 0.0

    def __enter__(self):
        self.t0 = time.perf_counter()
        log(self.event, phase="start", **self.fields)
        return self

    def __exit__(self, *exc_info) -> bool:
        log(self.event, phase="done",
            dur_ms=f"{(time.perf_counter() - self.t0) * 1000:.1f}", **self.fields)
        return False


# --- связка «нажатие мыши → контекстное меню» ---


def mark_press(button: str, x: float | None = None, y: float | None = None) -> None:
    """Запомнить нажатие: по нему считается ожидание события и попадание в меню."""
    global _last_press, _last_press_pos
    _last_press = (time.perf_counter(), button)
    _last_press_pos = (x, y)


def press_wait() -> float | None:
    """Сколько мс прошло с последнего нажатия мыши. None — нажатия не было."""
    if _last_press is None:
        return None
    return (time.perf_counter() - _last_press[0]) * 1000


def trace_menu(menu, where: str) -> None:
    """Повесить на меню лог момента показа и фактических габаритов.

    `ctx_show` — сколько заняла сборка и сколько ждал клик; `ctx_rect` — где меню реально
    оказалось и попадает ли в него последнее нажатие (по нему видно, был ли закрывающий
    клик мимо меню).
    """
    if not enabled():
        return
    t0 = time.perf_counter()

    def _log_rect() -> None:
        g = menu.geometry()
        px = py = None
        if _last_press is not None:
            px, py = _last_press_pos
        inside = ("-" if px is None else
                  g.x() <= px <= g.x() + g.width() and g.y() <= py <= g.y() + g.height())
        log("ctx_rect", where=where, x=g.x(), y=g.y(), w=g.width(), h=g.height(),
            visible=menu.isVisible(), press_x=px, press_y=py, press_inside=inside)

    def _on_show() -> None:
        log("ctx_show", where=where,
            build_ms=f"{(time.perf_counter() - t0) * 1000:.1f}",
            wait_ms=fmt_ms(press_wait()))
        # Геометрия окончательна только после показа — снимаем следующим тиком.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, _log_rect)

    menu.aboutToShow.connect(_on_show)


def chosen_label(action) -> str:
    return "-" if action is None else repr(action.text())


# --- установка в приложение -------------------------------------------------------


def install(app, stall_ms: float = 300.0, tick_ms: int = 100) -> bool:
    """Включить диагностику: фильтр нажатий и сторож главного потока.

    `stall_ms` — с какого простоя главного потока писать `stall` (таймер на занятом
    потоке срабатывает позже, поэтому зазор между тиками и есть время занятости).
    """
    if not enabled():
        return False

    from PySide6.QtCore import QEvent, QObject, QTimer

    class _PressFilter(QObject):
        def eventFilter(self, obj, event):  # noqa: N802
            if event.type() == QEvent.MouseButtonPress:
                # ВАЖНО: int(event.button()) в PySide6 6.9.3 роняет процесс (segfault на
                # этой сборке), поэтому берём .value у enum и int() не вызываем.
                name = _BUTTONS.get(event.button().value, "?")
                pos = event.globalPosition()
                mark_press(name, pos.x(), pos.y())
                log("press", button=name, x=pos.x(), y=pos.y())
            return False

    press_filter = _PressFilter(app)
    app.installEventFilter(press_filter)

    def _tick() -> None:
        global _last_tick
        now = time.perf_counter()
        if _last_tick is not None:
            gap = (now - _last_tick) * 1000
            if gap >= stall_ms:
                log("stall", gap_ms=f"{gap:.0f}")
        _last_tick = now

    timer = QTimer(app)
    timer.setInterval(tick_ms)
    timer.timeout.connect(_tick)
    timer.start()

    # удержать ссылки: без них Qt соберёт фильтр и таймер сборщиком мусора
    app._dbg_press_filter = press_filter
    app._dbg_stall_timer = timer
    log("dbg", phase="installed", log=log_path(), stall_ms=stall_ms)
    return True
