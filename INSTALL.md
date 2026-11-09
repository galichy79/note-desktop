# Установка QtNotes на новый компьютер и перенос заметок

Инструкция для Linux Mint 22 и других систем на базе Ubuntu 24.04 (x86_64). Команды
выполняются на новом компьютере, если не сказано иное.

## 1. Системные пакеты

```bash
sudo apt update
sudo apt install -y git python3-venv \
  libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 \
  libxcb-randr0 libxcb-render0 libxcb-shape0 libxcb-shm0 libxcb-sync1 libxcb-xfixes0 \
  libxcb-xkb1 libxkbcommon0 libxkbcommon-x11-0 libegl1 libgl1 libdbus-1-3 \
  libfontconfig1 libpulse0
```

Версии, с которыми приложение проверено (Linux Mint 22.1). Более новые версии из
репозитория системы подходят, вручную закреплять их не нужно.

| Пакет | Версия | Зачем |
|---|---|---|
| `git` | 2.43.0 | клонирование и обновление |
| `python3-venv` | 3.12.3 | виртуальное окружение |
| `libxcb-cursor0` | 0.1.4 | запуск Qt под X11, без него окно не откроется |
| `libxcb-icccm4` | 0.4.1 | Qt под X11 |
| `libxcb-image0` | 0.4.0 | Qt под X11 |
| `libxcb-keysyms1` | 0.4.0 | Qt под X11 |
| `libxcb-render-util0` | 0.3.9 | Qt под X11 |
| `libxcb-randr0`, `libxcb-render0`, `libxcb-shape0`, `libxcb-shm0`, `libxcb-sync1`, `libxcb-xfixes0`, `libxcb-xkb1` | 1.15 | Qt под X11 |
| `libxkbcommon0`, `libxkbcommon-x11-0` | 1.6.0 | клавиатура |
| `libegl1`, `libgl1` | 1.7.0 | OpenGL |
| `libdbus-1-3` | 1.14.10 | интеграция с рабочим столом |
| `libfontconfig1` | 2.15.0 | шрифты |
| `libpulse0` | 16.1 | звук в превью видео (QtMultimedia) |

Необязательно: `tpm2-tools` и устройство TPM 2.0 для аппаратного гейта ПИН-кода. Без них
приложение использует программный гейт.

## 2. Python

```bash
python3 --version
```

| Версия | Статус |
|---|---|
| 3.12.3 | проверена: чистый клон, установка зависимостей, `tests/smoke.py`. Системный Python в Mint 22 |
| 3.11.17 | основная версия разработки, на ней идёт CI |
| другая | не проверялась, поставьте 3.11 |

Python 3.11 ставится из PPA, в репозиториях Ubuntu 24.04 его нет:

```bash
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.11 python3.11-venv
```

Тогда на шаге 3 вместо `python3` используйте `python3.11`.

## 3. Приложение

```bash
cd ~
git clone https://github.com/galichy79/note-desktop.git
cd note-desktop
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run.sh
```

`pip` ставит такие версии. Прямые зависимости закреплены в `requirements.txt`, остальные
подтягиваются автоматически.

| Пакет | Версия | Источник |
|---|---|---|
| `PySide6-Essentials` | 6.9.3 | `requirements.txt` |
| `PySide6-Addons` | 6.9.3 | `requirements.txt` |
| `rapidfuzz` | 3.10.1 | `requirements.txt` |
| `python-dateutil` | 2.9.0.post0 | `requirements.txt` |
| `cryptography` | 49.0.0 | `requirements.txt` |
| `zeroconf` | 0.149.16 | `requirements.txt` |
| `segno` | 1.6.6 | `requirements.txt` |
| `shiboken6` | 6.9.3 | зависимость PySide6 |
| `cffi` | 2.1.1 | зависимость cryptography |
| `pycparser` | 3.0 | зависимость cffi |
| `ifaddr` | 0.2.0 | зависимость zeroconf |
| `six` | 1.17.0 | зависимость python-dateutil |

## 4. Ярлык в меню

```bash
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/qtnotes.desktop <<EOF
[Desktop Entry]
Type=Application
Name=QtNotes
Exec=$HOME/note-desktop/run.sh
Icon=$HOME/note-desktop/qtnotes/resources/icon.png
StartupWMClass=QtNotes
Terminal=false
EOF
```

Автозапуск при входе в систему (необязательно):

```bash
mkdir -p ~/.config/autostart
cp ~/.local/share/applications/qtnotes.desktop ~/.config/autostart/
```

## 5. Перенос заметок

Заметок в репозитории нет. Они переносятся архивом: папки, заметки, вложения и календарь.

**Настройки синхронизации и ключи этот способ не переносит, так и должно быть.**
Настройки приложения (шрифт, размер) задаются заново.

1. На старом компьютере нажмите кнопку «Добавить» в боковой панели и выберите «Экспортировать всё…».
   Получится файл `qtnotes_backup.zip`.
2. Перенесите файл на новый компьютер, например на флешке.
3. На новом компьютере в том же меню кнопки «Добавить» выберите «Импортировать архив…».
4. Сверьте число папок и заметок, откройте заметку с вложением.
5. Удалите архив с флешки и с обоих компьютеров.

> **Важно.** Архив содержит заметки и вложения в открытом виде. Не кладите его в этот
> репозиторий: репозиторий публичный.

Импорт объединяет архив с тем, что уже есть в хранилище. Файл с тем же именем
перезаписывается, поэтому архив лучше импортировать на чистой установке.

Не копируйте с другого компьютера каталог `~/.config/QtNotes/` и файл
`~/.local/share/QtNotes/sync.sqlite`. В них личность устройства и журнал синхронизации:
копия превратит два компьютера в одно устройство с точки зрения синхронизации.

## 6. Синхронизация с телефоном

Сопрягать можно только компьютер с телефоном: компьютер показывает QR, телефон его
сканирует. Прямого сопряжения двух компьютеров в приложении нет.

Каждый компьютер сопрягается с телефоном отдельно. Откройте настройки, затем диалог
«Синхронизация», включите её, нажмите «Сопрячь устройство (показать QR)» и отсканируйте код в приложении
Note на телефоне. Устройства должны быть в одной локальной сети.

## 7. Обновление

```bash
cd ~/note-desktop
git pull
.venv/bin/pip install -r requirements.txt
```

## Если что-то не работает

| Симптом | Что делать |
|---|---|
| `Could not load the Qt platform plugin "xcb"` | Проверьте пакеты из шага 1. Подробности покажет `QT_DEBUG_PLUGINS=1 ./run.sh` |
| `venv не найден` при запуске `./run.sh` | Выполните команды создания `.venv` из шага 3 |
| `pip install` падает на сборке пакета | Проверьте версию Python, поставьте 3.11 (шаг 2) |
| Телефон не находит компьютер | Проверьте общую сеть и файрвол: нужны mDNS (UDP 5353) и порт синхронизации из `~/.config/QtNotes/settings.json` (`sync_port`) |

## Где что лежит

| Что | Путь |
|---|---|
| Приложение | `~/note-desktop` |
| Заметки | `~/.local/share/QtNotes` (меняется переменной `QTNOTES_VAULT`) |
| Настройки и личность устройства | `~/.config/QtNotes` |
