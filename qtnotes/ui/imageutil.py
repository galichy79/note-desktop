"""Декод изображений для интерфейса: сразу в размер показа и с EXIF-ориентацией.

Зачем отдельный модуль: полный декод крупного фото — единственная по-настоящему
дорогая операция в UI (замер на этой машине: JPEG 4080x3060 — ~200 мс и ~50 МБ на
пиксмап), а показывается такое фото шириной максимум `IMG_MAX_W`. `setScaledSize`
снимает и время (~75 мс, в 2,6 раза), и память (~1,5 МБ). Поэтому полный кадр
декодируется ТОЛЬКО по явному запросу (копирование в буфер), а всё, что рисуется
на экране, идёт через `decode_display`.

EXIF-ориентация: ни `QPixmap`, ни `QImage` её не применяют сами — фото с камеры
(Orientation=6) показывалось повёрнутым на 90°. Здесь она применяется везде
(`setAutoTransform`), а размеры считает `oriented_size`, потому что
`QImageReader.size()` ориентацию НЕ учитывает (для нашего фото отдаёт 4080x3060,
тогда как декод даёт 3060x4080) — без этого заглушка разошлась бы с картинкой.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QImageReader, QPixmap

# QImageIOHandler.Transformation: 4=Rotate90, 5=Mirror+Rotate90, 6=Flip+Rotate90,
# 7=Rotate270. PySide6 6.9.3 отдаёт не все члены перечисления атрибутами класса,
# поэтому — числами; читать надо `.value` (int() по enum здесь падает).
_ROTATING_TRANSFORMS = frozenset({4, 5, 6, 7})


def _reader(path) -> QImageReader:
    r = QImageReader(str(path))
    r.setAutoTransform(True)
    return r


def oriented_size(path) -> tuple[int, int]:
    """Размер картинки ПОСЛЕ применения EXIF-ориентации; (0, 0) — не прочитался."""
    r = _reader(path)
    size = r.size()
    if not size.isValid():
        return 0, 0
    if r.transformation().value in _ROTATING_TRANSFORMS:
        size.transpose()      # поворот на 90° меняет местами ширину и высоту
    return size.width(), size.height()


def decode_display(path, max_side: int) -> QPixmap:
    """Декодировать картинку сразу уменьшенной: большая сторона ≤ max_side.

    Картинки, которые и так меньше — декодируются как есть (не растягиваем).
    Пустой QPixmap — файл не прочитался (вызывающий показывает плашку).
    """
    r = _reader(path)
    final_w, final_h = oriented_size(path)
    if final_w and final_h and max(final_w, final_h) > max_side:
        k = max_side / max(final_w, final_h)
        raw = r.size()                     # размеры ДО поворота — их и масштабируем
        r.setScaledSize(QSize(max(1, round(raw.width() * k)),
                              max(1, round(raw.height() * k))))
    img = r.read()
    return QPixmap.fromImage(img) if not img.isNull() else QPixmap()


def full_pixmap(path) -> QPixmap:
    """Полный кадр с EXIF-ориентацией — для буфера обмена (дорого: ~200 мс на 12 МП).

    Вызывать только по явному действию пользователя, не на отрисовке.
    """
    img = _reader(path).read()
    return QPixmap.fromImage(img) if not img.isNull() else QPixmap()
