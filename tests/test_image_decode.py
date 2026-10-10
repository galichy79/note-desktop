"""Декод картинок: размер показа вместо полного кадра + EXIF-ориентация.

Ловит две вещи, которые уже ломались:
  * полный декод фото 12 МП ради картинки шириной 720 (это ~200 мс фриза UI и
    ~50 МБ на пиксмап — см. tools/perf_bench.py);
  * игнорирование EXIF-ориентации: Qt её не применяет ни в QPixmap, ни в QImage,
    поэтому фото с камеры (Orientation=6) показывалось повёрнутым на 90°, а
    заглушка в ленте резервировала размеры «лёжа».

Крохотный JPEG с Orientation=6 вложен прямо сюда (base64): тесты идут без Pillow.

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_image_decode.py
"""

import base64
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# ВАЖНО: изолированные временные vault и config — тест создаёт вложения и заметку
# (импорт), реальные данные пользователя трогать нельзя. Задаём до импорта qtnotes.
os.environ["QTNOTES_VAULT"] = tempfile.mkdtemp(prefix="qtnotes_img_vault_")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="qtnotes_img_cfg_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

# JPEG 40x20 (альбомный) с EXIF Orientation=6 → показывать надо 20x40 (портрет).
JPEG_EXIF6 = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/4QAiRXhpZgAATU0AKgAAAAgAAQESAAMAAAABAAYAAAAAAAD/"
    "2wBDAAUDBAQEAwUEBAQFBQUGBwwIBwcHBw8LCwkMEQ8SEhEPERETFhwXExQaFRERGCEYGh0dHx8f"
    "ExciJCIeJBweHx7/2wBDAQUFBQcGBw4ICA4eFBEUHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4e"
    "Hh4eHh4eHh4eHh4eHh4eHh4eHh4eHh7/wAARCAAUACgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEA"
    "AAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJx"
    "FDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNk"
    "ZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJ"
    "ytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQF"
    "BgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMz"
    "UvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3"
    "eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna"
    "4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDzSiiivkT+iAooooAKKKKACiiigAooooAK"
    "KKKAP//Z"
)


def main() -> None:
    app = QApplication.instance() or QApplication([])
    base = tempfile.mkdtemp(prefix="qtnotes_img_")
    try:
        from qtnotes.ui.imageutil import decode_display, full_pixmap, oriented_size

        path = os.path.join(base, "photo.jpg")
        with open(path, "wb") as f:
            f.write(JPEG_EXIF6)

        # 1. EXIF-ориентация: заголовок отдаёт альбомные 40x20, показывать надо 20x40
        raw_w, raw_h = 40, 20
        w, h = oriented_size(path)
        assert (w, h) == (raw_h, raw_w), f"ориентация не применена: {w}x{h}"
        print(f"OK: oriented_size = {w}x{h} (заголовок рапортует {raw_w}x{raw_h})")

        # 2. декод в размер показа: портрет, большая сторона не больше запрошенной
        pm = decode_display(path, 20)
        assert pm.height() > pm.width(), f"картинка повёрнута неверно: {pm.width()}x{pm.height()}"
        assert max(pm.width(), pm.height()) <= 20, f"декод больше запрошенного: {pm.width()}x{pm.height()}"
        print(f"OK: decode_display = {pm.width()}x{pm.height()} — портрет, в пределах цели")

        # 3. полный кадр (для буфера обмена) — тоже с ориентацией
        fp = full_pixmap(path)
        assert (fp.width(), fp.height()) == (raw_h, raw_w), f"буфер получил бы {fp.width()}x{fp.height()}"
        print(f"OK: full_pixmap = {fp.width()}x{fp.height()} — полный кадр, портрет")

        # 4. заглушка пузыря совпадает с картинкой (иначе раскладка поедет)
        from qtnotes.storage.models import Attachment, Note
        from qtnotes.ui.message_bubble import MessageBubble
        note = Note.create_text("f" * 32, "<p>подпись</p>", "подпись")
        note.attachments = [Attachment(file="photo.jpg", mime="image/jpeg", name="photo.jpg",
                                       size=len(JPEG_EXIF6))]
        # вложение лежит не в блобе, а рядом с заметкой — как в режиме без синка
        from qtnotes.storage import vault
        adir = vault.attachments_dir(note.folder_id, note.id)
        adir.mkdir(parents=True, exist_ok=True)
        with open(adir / "photo.jpg", "wb") as f:
            f.write(JPEG_EXIF6)

        bubble = MessageBubble(note)
        slots = [s for s in bubble._media if s["kind"] == "image"]
        assert slots, "не создан слот изображения"
        slot = slots[0]
        assert (slot["w"], slot["h"]) == (raw_h, raw_w), \
            f"заглушка {slot['w']}x{slot['h']} не совпадает с картинкой"
        bubble.set_active(True)
        app.processEvents()
        shown = slot["widget"].pixmap()
        assert shown.height() > shown.width(), f"в ленте картинка лежит боком: {shown.width()}x{shown.height()}"
        print(f"OK: заглушка {slot['w']}x{slot['h']}, в ленте {shown.width()}x{shown.height()}")

        # 5. импорт запоминает размеры С ориентацией
        from qtnotes.ui.chat_view import _build_pending_note
        note2 = _build_pending_note(
            "f" * 32,
            [{"id": "p1", "type": "path", "path": path, "name": "photo.jpg"}],
            "подпись", "<p>подпись</p>")
        att = note2.attachments[0]
        assert (att.w, att.h) == (raw_h, raw_w), f"импорт записал {att.w}x{att.h}"
        print(f"OK: импорт сохранил {att.w}x{att.h} — с ориентацией")

        print("\nДЕКОД КАРТИНОК: ОРИЕНТАЦИЯ И РАЗМЕР ПОКАЗА СОШЛИСЬ")
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
