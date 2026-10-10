"""Контекстное меню медиа-элемента: копировать в буфер / копировать путь."""

from __future__ import annotations

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMenu

from .. import dbg


def show_media_menu(widget, global_pos, path, pixmap=None, where: str = "media",
                    image_loader=None) -> bool:
    """Показать меню для медиа.

    `pixmap` — готовая картинка для пункта «Копировать изображение».
    `image_loader` — вызываемый без аргументов, возвращающий картинку: пункт
    показывается сразу, а сам декод идёт ТОЛЬКО при выборе. Для фото 12 МП это
    ~200 мс, и тратить их до показа меню незачем (меню открывалось с задержкой).

    Возвращает True, если меню показано/обработано.
    """
    menu = QMenu(widget)
    act_img = None
    if image_loader is not None or (pixmap is not None and not pixmap.isNull()):
        act_img = menu.addAction("Копировать изображение")
    act_file = menu.addAction("Копировать файл")
    act_path = menu.addAction("Копировать путь к файлу")
    dbg.trace_menu(menu, where)
    chosen = menu.exec(global_pos)
    dbg.log("ctx_done", where=where, chosen=dbg.chosen_label(chosen))
    if chosen is None:
        return True

    cb = QGuiApplication.clipboard()
    if act_img is not None and chosen == act_img:
        pm = image_loader() if image_loader is not None else pixmap
        if pm is not None and not pm.isNull():
            cb.setPixmap(pm)
    elif chosen == act_file:
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(str(path))])
        cb.setMimeData(md)
    elif chosen == act_path:
        cb.setText(str(path))
    return True
