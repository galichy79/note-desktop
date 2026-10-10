"""Замер узких мест QtNotes на живых данных.

Показывает, сколько времени уходит на декод изображений, постройку ленты,
чтение/запись заметок, индекс, поиск и запуск приложения — на реальном vault
пользователя. Годится как «до/после»: `--json` даёт машиночитаемый вывод,
чтобы сравнить прогоны.

Запуск:
    .venv/bin/python tools/perf_bench.py                # только чтение
    .venv/bin/python tools/perf_bench.py --write        # + запись в живой vault
    .venv/bin/python tools/perf_bench.py --runs 7 --json

Пишет ТОЛЬКО с `--write` и всегда удаляет за собой созданную заметку. Без
флага живой vault не трогается: идут только чтения (list/load/index/search).

Ничего не оптимизирует и не патчит — измеряет. Приложение и его настройки не
меняет, работает в offscreen-режиме (нужен только Qt, окно не открывается).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # до импорта QApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QSize, Qt  # noqa: E402
from PySide6.QtGui import QImageReader, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

RESULTS: dict[str, float] = {}


def bench(label: str, fn, runs: int = 5) -> float:
    """Прогнать fn runs раз, напечатать min/median, вернуть медиану (мс)."""
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    med = statistics.median(times)
    print(f"{label:62s} min={min(times):8.1f}  med={med:8.1f} ms")
    return med


def note_result(key: str, label: str, value: float, unit: str = "ms") -> None:
    RESULTS[key] = round(value, 2)
    print(f"{label:62s} {value:8.1f} {unit}")


# --- поиск реальных картинок в vault -----------------------------------------

def real_images(vault_mod) -> list[tuple[Path, int]]:
    """[(путь, число пикселей)] по вложениям всех заметок — картинки только.

    Путь берётся через attachment_access_path: если blob зашифрован, вернётся
    расшифрованный tmpfs-файл (тот же, что отдаётся виджетам)."""
    found: list[tuple[Path, int]] = []
    try:
        for folder in vault_mod.list_folders():
            for note in vault_mod.list_notes(folder.id):
                for att in note.attachments:
                    mime = att.mime or ""
                    if not mime.startswith("image/") or mime == "image/gif":
                        continue
                    path = vault_mod.attachment_access_path(note, att)
                    if path is None or not Path(path).is_file():
                        continue
                    size = QImageReader(str(path)).size()   # только заголовок, дёшево
                    px = size.width() * size.height() if size.isValid() else 0
                    found.append((Path(path), px))
    except Exception as exc:  # noqa: BLE001 — замер не должен падать из-за данных
        print(f"[!] не удалось прочитать вложения vault: {exc}")
    found.sort(key=lambda t: t[1], reverse=True)
    return found


def synth_jpeg(px_w: int = 4080, px_h: int = 3060) -> Path | None:
    """Синтетический JPEG заданного размера — запасной вариант, если своих фото нет."""
    try:
        from PIL import Image
    except ImportError:
        return None
    d = Path(tempfile.mkdtemp(prefix="qtnotes-bench-"))
    out = d / f"synth_{px_w}x{px_h}.jpg"
    img = Image.new("RGB", (px_w, px_h))
    px = img.load()
    for y in range(0, px_h, 3):            # немного шума, чтобы файл не жался в ноль
        for x in range(0, px_w, 3):
            px[x, y] = ((x * 7) % 256, (y * 5) % 256, ((x + y) * 3) % 256)
    img.save(out, "JPEG", quality=92)
    return out


# --- секции ------------------------------------------------------------------

def section_images(runs: int, images: list[tuple[Path, int]], vault_mod, qapp) -> None:
    print("=" * 96)
    print("1. ДЕКОД ИЗОБРАЖЕНИЙ  (единственное место, где заметно слабое железо)")
    print("=" * 96)

    if images:
        path, px = images[0]
        kind = "вложение из vault"
    else:
        path = synth_jpeg()
        if path is None:
            print("[!] в vault нет картинок, и Pillow недоступен — секция пропущена")
            return
        px = 4080 * 3060
        kind = "синтетический JPEG (своих фото нет)"

    data = Path(path).read_bytes()
    mp = px / 1e6
    print(f"файл: {Path(path).name}  ({mp:.1f} МП, {len(data) / 1e6:.1f} МБ, {kind})")
    print()

    def full_decode():
        pm = QPixmap()
        pm.loadFromData(data)
        return pm

    note_result("decode_full_ms",
                "полный декод — столько стоило бы читать кадр целиком",
                bench("  QPixmap.loadFromData — полный декод", full_decode, runs))

    def decode_scaled(width: int):
        r = QImageReader(str(path))
        r.setAutoTransform(True)
        size = r.size()
        if size.width() > width:
            h = max(1, round(size.height() * width / size.width()))
            r.setScaledSize(QSize(width, h))
        return r.read()

    note_result("decode_scaled720_ms",
                "  декод сразу уменьшенным (так делает приложение сейчас)",
                bench("  QImageReader.setScaledSize(720)", lambda: decode_scaled(720), runs))

    def full_then_scale():
        pm = QPixmap()
        pm.loadFromData(data)
        return pm.scaledToWidth(720, Qt.SmoothTransformation)

    bench("  декод + scaledToWidth(720)", full_then_scale, runs)

    size = QImageReader(str(path)).size()
    if size.isValid():
        print(f"{'  размер по заголовку (EXIF не применён)':62s} "
              f"{size.width()}x{size.height()}")
        r = QImageReader(str(path))
        r.setAutoTransform(True)
        r.setScaledSize(QSize(720, max(1, round(size.height() * 720 / size.width()))))
        img = r.read()
        if not img.isNull():
            print(f"{'  размер после setAutoTransform':62s} "
                  f"{img.width()}x{img.height()}")
        print(f"  память полного пиксмапа: "
              f"{size.width() * size.height() * 4 / 1e6:.0f} МБ, "
              f"уменьшенного до 720: ~{720 * round(size.height() * 720 / size.width()) * 4 / 1e6:.1f} МБ")
    print()

    # обои: декод + размытие, как делает ChatView._make_blurred
    from qtnotes.ui.theme import wallpaper_path
    wp = wallpaper_path()
    if wp and Path(wp).is_file():
        wp_bytes = Path(wp).read_bytes()

        def wall_path():
            wall = QPixmap()
            wall.loadFromData(wp_bytes)
            size = QSize(1280, 800)
            cover = wall.scaled(size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            small = QSize(max(1, cover.width() // 14), max(1, cover.height() // 14))
            blurred = cover.scaled(small, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            return blurred.scaled(cover.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)

        bench("  обои: декод PNG + cover + размытие 1280x800", wall_path, max(3, runs // 2))
    print()


def section_feed(runs: int, bubbles_n: int, images: list[tuple[Path, int]],
                 vault_mod, config_mod) -> None:
    print("=" * 96)
    print("2. ЛЕНТА: постройка пузырей и активация медиа")
    print("=" * 96)

    from qtnotes.storage.models import Attachment, Note
    from qtnotes.ui.message_bubble import MessageBubble

    body = ("<p>Заметка для замера: текст, <a href='https://example.com'>ссылка</a>, "
            "упоминание [[a1b2c3d4e5f60718293a4b5c6d7e8f90]] и ещё немного текста "
            "для переноса строк.</p>")

    def text_note(i: int) -> Note:
        return Note.create_text("f" * 32, body * 3, "Заметка для замера " * 40)

    t0 = time.perf_counter()
    keep = [MessageBubble(text_note(i)) for i in range(bubbles_n)]
    total = (time.perf_counter() - t0) * 1000
    note_result("bubbles_ms", f"постройка {bubbles_n} текстовых пузырей (всего)", total)
    print(f"{'  на один пузырь':62s} {total / bubbles_n:8.2f} ms")
    del keep

    if images:
        path, px = images[0]
        att = Attachment(file=Path(path).name, mime="image/jpeg", name=Path(path).name,
                         size=Path(path).stat().st_size, w=0, h=0,
                         sha256=Path(path).name if _looks_like_blob(path, config_mod) else "")
        note = text_note(0)
        note.attachments = [att]
        if not att.sha256:                     # не blob — подложим прямой путь
            att.file = str(path)
        bubble = MessageBubble(note)

        def activate():
            bubble.set_active(False)
            bubble.set_active(True)

        note_result("bubble_activate_photo_ms",
                    f"активация пузыря с фото ({px / 1e6:.1f} МП) — что делает приложение",
                    bench("  set_active(True) с картинкой", activate, runs))
    print()


def _looks_like_blob(path: Path, config_mod) -> bool:
    try:
        return Path(path).resolve().parent == Path(config_mod.blobs_dir()).resolve()
    except OSError:
        return False


def section_storage(runs: int, vault_mod, index_mod, search_mod, write: bool) -> None:
    print("=" * 96)
    print("3. ХРАНИЛИЩЕ: чтение, индекс, поиск" + (", запись" if write else " (без записи)"))
    print("=" * 96)

    folders = vault_mod.list_folders()
    notes = []
    for f in folders:
        notes.extend(vault_mod.list_notes(f.id))
    print(f"данные: папок={len(folders)} заметок={len(notes)} "
          f"вложений={sum(len(n.attachments) for n in notes)}")
    print()

    def list_all():
        for f in folders:
            vault_mod.list_notes(f.id)

    note_result("read_all_notes_ms", "list_notes() по всем папкам",
                bench("  чтение всех заметок (JSON)", list_all, runs))
    note_result("index_ready_ms", "index.ensure_ready()",
                bench("  готовность индекса", index_mod.ensure_ready, runs))
    note_result("index_rebuild_ms", "index.rebuild() (полная перестройка FTS)",
                bench("  перестройка индекса", index_mod.rebuild, max(3, runs // 2)))

    # запрос берём из живых данных, чтобы поиск реально что-то находил
    words = []
    for n in notes:
        for w in (n.plaintext or "").split():
            if len(w) > 4:
                words.append(w.strip(".,!?:;()»«\""))
    query = words[0] if words else "заметка"
    note_result("search_ms", f"search.search({query!r})",
                bench("  поиск", lambda: search_mod.search(query), runs))

    if not write:
        print("  (запись не замеряется: нужен флаг --write — он создаёт и удаляет"
              " заметку в ЖИВОМ vault)")
        print()
        return

    if not folders:
        print("  [!] папок нет — запись пропущена")
        print()
        return

    print("  [!] --write: создаю и сразу удаляю заметку в НАСТРОЕННОМ vault"
          " (по умолчанию — живой; для безопасной проверки задайте QTNOTES_VAULT)")
    fid = folders[0].id
    plain = "замер записи заметки " * 40
    from qtnotes.storage.models import Note

    def save_once():
        n = Note.create_text(fid, f"<p>{plain}</p>", plain)
        vault_mod.save_note(n)
        vault_mod.delete_note(n)

    note_result("save_note_ms", "save_note + delete_note",
                bench("  запись и удаление заметки", save_once, runs))
    print()


def section_startup(runs: int) -> None:
    print("=" * 96)
    print("4. ЗАПУСК: фазы старта (в отдельном процессе, как в жизни)")
    print("=" * 96)
    # Изолированные vault/config/cache: старт меряется на пустом хранилище с
    # выключенным синком — живой vault и движок не трогаем.
    base = tempfile.mkdtemp(prefix="qtnotes-bench-boot-")
    env = dict(os.environ,
               QT_QPA_PLATFORM="offscreen",
               QTNOTES_VAULT=f"{base}/vault",
               XDG_CONFIG_HOME=f"{base}/cfg",
               XDG_CACHE_HOME=f"{base}/cache")
    # Отдельный файл, а не `-c`: многострочный код с классом и циклом в одну
    # строку не влезает без ловушек с отступами.
    probe = Path(base) / "boot_probe.py"
    probe.write_text(f'''
"""Замер фаз старта: от запуска процесса до первого кадра окна."""
import sys, time
sys.path.insert(0, {str(ROOT)!r})
t0 = time.perf_counter()
from PySide6.QtWidgets import QApplication
app = QApplication([])
t1 = time.perf_counter()
from qtnotes.ui.fonts import ensure_emoji_font
ensure_emoji_font()
from qtnotes.ui.theme import build_qss
from qtnotes.ui.main_window import MainWindow, apply_font
t2 = time.perf_counter()
app.setStyleSheet(build_qss())
apply_font(app)
t3 = time.perf_counter()

from PySide6.QtCore import QEvent, QObject


class Watch(QObject):
    """Ловит первую отрисовку окна: по ней видно, когда окно реально на экране."""

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Paint and obj is win and not painted:
            painted.append(time.perf_counter())
        return False


painted = []
win = MainWindow()
t4 = time.perf_counter()
watch = Watch()          # ссылку ДЕРЖИМ: PySide6 не владеет объектом фильтра
win.installEventFilter(watch)
win.show()
t5 = time.perf_counter()
deadline = time.monotonic() + 5
while not painted and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.005)
t6 = time.perf_counter()
print(f"{{(t1 - t0) * 1000:.1f}} {{(t2 - t1) * 1000:.1f}} {{(t3 - t2) * 1000:.1f}} "
      f"{{(t4 - t3) * 1000:.1f}} {{(t5 - t4) * 1000:.1f}} {{(t6 - t0) * 1000:.1f}} "
      f"{{len(painted)}}")
''', encoding="utf-8")
    times = []
    for _ in range(runs):
        out = subprocess.run([sys.executable, str(probe)], capture_output=True,
                             text=True, timeout=300, env=env)
        if out.returncode != 0:
            print(f"[!] старт не удалось замерить: {out.stderr.strip()[:300]}")
            shutil.rmtree(base, ignore_errors=True)
            return
        times.append([float(x) for x in out.stdout.split()])
    qt = statistics.median(t[0] for t in times)
    mods = statistics.median(t[1] for t in times)
    qss = statistics.median(t[2] for t in times)
    win_ms = statistics.median(t[3] for t in times)
    show_ms = statistics.median(t[4] for t in times)
    total = statistics.median(t[5] for t in times)
    painted = min(t[6] for t in times)
    print(f"{'  QApplication (импорт PySide6 + окно-обвязка)':62s} {qt:8.1f} ms")
    print(f"{'  импорт модулей qtnotes.ui':62s} {mods:8.1f} ms")
    print(f"{'  тема (build_qss + шрифт)':62s} {qss:8.1f} ms")
    print(f"{'  MainWindow() — постройка окна':62s} {win_ms:8.1f} ms")
    print(f"{'  show()':62s} {show_ms:8.1f} ms")
    if not painted:
        print("  [!] окно не отрисовалось за 5 с — ИТОГО ниже не о появлении окна")
    note_result("startup_ms", "  ИТОГО до ПЕРВОГО КАДРА окна (без разблокировки)",
                total)
    shutil.rmtree(base, ignore_errors=True)
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Замер узких мест QtNotes")
    ap.add_argument("--runs", type=int, default=5, help="прогонов на замер (по умолчанию 5)")
    ap.add_argument("--bubbles", type=int, default=49, help="сколько пузырей строить в ленте")
    ap.add_argument("--write", action="store_true",
                    help="замерить запись заметки (создаёт и удаляет её в ЖИВОМ vault)")
    ap.add_argument("--json", action="store_true", help="допечатать результаты в JSON")
    args = ap.parse_args()

    qapp = QApplication([])

    from qtnotes import config
    from qtnotes.storage import index, search, vault

    images = real_images(vault)
    section_images(args.runs, images, vault, qapp)
    section_feed(args.runs, args.bubbles, images, vault, config)
    section_storage(args.runs, vault, index, search, args.write)
    section_startup(max(2, args.runs // 2))

    print("=" * 96)
    print("СВОДКА (медианы, мс)")
    print("=" * 96)
    for key, value in sorted(RESULTS.items(), key=lambda kv: kv[1], reverse=True):
        print(f"{key:28s} {value:9.1f}")
    print()
    print("Ориентир: узкое место — единственное, что заметно выше ~50 мс. Всё остальное")
    print("в приложении работает за единицы миллисекунд, оптимизировать там нечего.")

    if args.json:
        print()
        print(json.dumps(RESULTS, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
