"""Жизненный цикл движка синхронизации со стороны UI.

Держит SyncEngine (он крутится в своём потоке), маршалит уведомления об удалённых
изменениях в поток UI через Qt-сигналы и обслуживает сопряжение по QR. Локальные
изменения авто-пушатся через хук oplog.set_change_listener (развязка с vault).
"""

from __future__ import annotations

import asyncio

from PySide6.QtCore import QObject, Qt, Signal

from .. import config
from ..sync import identity as identity_mod
from ..sync import peers as peers_mod


class SyncManager(QObject):
    remoteChanged = Signal()      # применены удалённые изменения (доставка в UI-поток)
    devicePaired = Signal(str)    # имя/ид сопряжённого устройства
    statusChanged = Signal()      # включение/выключение/состояние сессий
    # сопряжение компьютеров по коду
    codeOfferReady = Signal()              # предложение слушает, можно вводить код
    codeSasReady = Signal(str, str)        # контрольное число, имя второго ПК
    codePairFinished = Signal(bool, str)   # успех; подпись устройства или причина
    _codePairDone = Signal(bool, str)      # из потока движка в поток UI

    def __init__(self, parent=None):
        super().__init__(parent)
        self._engine = None
        self._identity = None
        self._pairing_server = None
        self._code_offer = None
        self._code_announcer = None
        self._code_join = None
        self._code_sas = None
        self._code_label = ""
        self._code_generation = 0     # отменённая попытка не должна сообщать об исходе
        self._codePairDone.connect(self._on_code_pair_done, Qt.QueuedConnection)

    def identity(self):
        if self._identity is None:
            self._identity = identity_mod.ensure_identity()
        return self._identity

    def is_running(self) -> bool:
        return self._engine is not None

    # --- старт/стоп ---

    def start(self) -> None:
        if self._engine is not None:
            return
        from ..sync import engine as engine_mod
        from ..sync import oplog
        from ..sync import seed
        from ..sync import store as store_mod

        from ..sync import theme_publish

        ident = self.identity()

        def _on_started():
            seed.ensure_seeded()          # миграция вложений в blobs
            theme_publish.publish_theme()  # опубликовать тему+обои для телефона
            oplog.compact()               # B1: подрезать историю до старта синка с пирами

        # тяжёлый старт отдаём движку — он выполнит его в своём потоке после старта
        # сервера, чтобы не морозить UI. Порт — сохранённый (стабильный между
        # перезапусками), чтобы адрес в QR не устаревал.
        from .. import config
        self._engine = engine_mod.SyncEngine(
            ident, store_mod.GlobalStore(),
            on_changed=self._on_remote_changed, on_started=_on_started,
            serve_port=config.sync_port())
        oplog.set_change_listener(self._on_local_change)
        self._engine.start()
        # запомнить фактический порт (может отличаться, если сохранённый был занят)
        if self._engine._port:
            config.set_sync_port(self._engine._port)
        self.statusChanged.emit()

    def stop(self) -> None:
        from ..sync import oplog
        oplog.set_change_listener(None)
        if self._engine is not None:
            self._engine.stop()
            self._engine = None
        self.statusChanged.emit()

    def restart(self) -> None:
        """Перезапустить движок (например, после сопряжения — чтобы сервер начал
        доверять новому устройству; trusted-cert фиксируется при bind)."""
        if self._engine is not None:
            self.stop()
            self.start()

    def set_enabled(self, on: bool) -> None:
        config.set_sync_enabled(on)
        if on:
            self.start()
        else:
            self.stop()

    # --- колбэки движка (приходят из его потока) ---

    def _on_remote_changed(self) -> None:
        self.remoteChanged.emit()   # очередь в UI-поток (обновить ленту/календарь)
        self.statusChanged.emit()   # и статус «онлайн устройств»

    def _on_local_change(self) -> None:
        if self._engine is not None:
            self._engine.notify_change()

    def online_peers(self) -> list[str]:
        if self._engine is None:
            return []
        return list(self._engine.sessions.keys())

    # --- сопряжение по QR (десктоп показывает, телефон сканирует) ---

    def start_pairing(self) -> str | None:
        """Поднять ОДНОРАЗОВЫЙ слушатель сопряжения, вернуть payload для QR. Нужен
        запущенный движок. Предыдущий слушатель (если был) закрывается."""
        if self._engine is None or self._engine._loop is None:
            return None
        from ..sync import discovery, pairing

        self.stop_pairing()  # не плодить параллельные слушатели/токены
        ident = self.identity()
        token = pairing.generate_token()

        def on_paired(peer):
            peers_mod.add_peer(peer.device_id, peer.name, peer.cert_pem)
            # показываем имя + device_id (= префикс отпечатка cert) для сверки
            label = f"{peer.name} · {peer.device_id}" if peer.name else peer.device_id
            self.devicePaired.emit(label)

        loop = self._engine._loop
        fut = asyncio.run_coroutine_threadsafe(
            pairing.serve_pairing(ident, token, on_paired=on_paired,
                                  host="0.0.0.0", port=0), loop)
        server = fut.result(timeout=5)
        self._pairing_server = server
        port = server.sockets[0].getsockname()[1]
        sync_port = self._engine._port or 0
        return pairing.make_pairing_payload(
            ident, discovery._primary_ip(), port, token, sync_port=sync_port)

    def stop_pairing(self) -> None:
        server = self._pairing_server
        self._pairing_server = None
        if server is not None and self._engine is not None and self._engine._loop is not None:
            self._engine._loop.call_soon_threadsafe(server.close)

    # --- сопряжение компьютеров по коду (см. sync/code_pairing.py) ---

    def _ensure_engine_loop(self):
        """Цикл движка; синхронизация включается, если была выключена."""
        if self._engine is None:
            if config.sync_enabled():
                self.start()
            else:
                self.set_enabled(True)
        return self._engine._loop if self._engine is not None else None

    def _save_code_peer(self, peer, host: str, sync_port: int) -> None:
        """Занести пира в доверенные. Вызывается в потоке движка."""
        peers_mod.add_peer(peer.device_id, peer.name, peer.cert_pem)
        if host and sync_port:
            # прямой адрес: пара заработает и там, где mDNS не проходит
            peers_mod.save_peer_addr(peer.device_id, host, sync_port)
        self._code_label = f"{peer.name} · {peer.device_id}" if peer.name else peer.device_id

    async def _decide_code_sas(self, sas: str, name: str) -> bool:
        """Показать контрольное число и дождаться решения человека."""
        future = asyncio.get_running_loop().create_future()
        self._code_sas = future
        self.codeSasReady.emit(sas, name)
        try:
            return await future
        finally:
            self._code_sas = None

    def answer_code_sas(self, matches: bool) -> None:
        """Ответ пользователя: совпало ли контрольное число."""
        if self._engine is None or self._engine._loop is None:
            return

        def resolve():
            future = self._code_sas
            if future is not None and not future.done():
                future.set_result(matches)

        self._engine._loop.call_soon_threadsafe(resolve)

    def _on_code_pair_done(self, ok: bool, info: str) -> None:
        """В потоке UI. После успеха сервер движка должен начать доверять новому ПК."""
        if ok:
            self.restart()
        self.codePairFinished.emit(ok, info)

    def start_code_offer(self) -> str | None:
        """Старый ПК: выдать код. Слушатель поднимается в фоне (см. codeOfferReady)."""
        from ..sync import code_pairing

        self.cancel_code_pairing()
        loop = self._ensure_engine_loop()
        if loop is None:
            return None
        code = code_pairing.generate_code()
        generation = self._code_generation
        ident = self.identity()

        def on_closed(reason: str) -> None:
            announcer, self._code_announcer = self._code_announcer, None
            if announcer is not None:
                # снятие анонса блокирует — не держим им цикл движка
                asyncio.ensure_future(asyncio.to_thread(announcer.stop))
            self._code_offer = None
            if generation != self._code_generation or reason == code_pairing.REASON_CANCELLED:
                return
            if reason == code_pairing.REASON_PAIRED:
                self._codePairDone.emit(True, self._code_label)
            else:
                self._codePairDone.emit(False, reason)

        async def run() -> None:
            try:
                offer = await code_pairing.serve_code_pairing(
                    ident, code, self._decide_code_sas, self._save_code_peer,
                    on_closed=on_closed, sync_port=self._engine._port or 0)
            except OSError:
                if generation == self._code_generation:
                    self._codePairDone.emit(False, code_pairing.REASON_NETWORK)
                return
            if generation != self._code_generation:
                offer.close()
                return
            self._code_offer = offer
            self.codeOfferReady.emit()
            announcer = code_pairing.new_announcer()
            self._code_announcer = announcer
            try:
                await asyncio.to_thread(announcer.start, ident, offer.port)
            except Exception:  # noqa: BLE001 — без mDNS новый ПК найдёт перебором подсети
                self._code_announcer = None
                return
            if self._code_offer is not offer:
                # предложение закрыли, пока шла регистрация: снять анонс сейчас
                self._code_announcer = None
                await asyncio.to_thread(announcer.stop)

        asyncio.run_coroutine_threadsafe(run(), loop)
        return code

    def join_code_offer(self, code: str) -> bool:
        """Новый ПК: найти предложение и подключиться. Исход — в codePairFinished."""
        from ..sync import code_pairing as cp

        self.cancel_code_pairing()
        loop = self._ensure_engine_loop()
        if loop is None:
            return False
        generation = self._code_generation
        ident = self.identity()

        async def attempt() -> str | None:
            """None — успех, иначе причина неудачи."""
            try:
                offers = await asyncio.to_thread(cp.discover_offers, ident.device_id)
            except Exception:  # noqa: BLE001 — сбой mDNS не повод сдаваться: есть перебор
                offers = []
            if not offers:
                offers = await cp.scan_offers()
            if not offers:
                return cp.REASON_NOT_FOUND
            reason = cp.REASON_NOT_FOUND
            for offer in offers:
                try:
                    await cp.join_code_pairing(
                        ident, code, offer.host, offer.port, self._decide_code_sas,
                        self._save_code_peer, sync_port=self._engine._port or 0)
                    return None
                except cp.CodePairingError as e:
                    reason = e.reason
                    if reason == cp.REASON_REJECTED:
                        break       # человек увидел несовпадение — дальше не пробуем
            return reason

        async def run() -> None:
            self._code_join = asyncio.current_task()
            try:
                reason = await attempt()
            except Exception:  # noqa: BLE001 — экран не должен зависнуть на «Ищу…»
                reason = cp.REASON_NETWORK
            finally:
                self._code_join = None
            if generation != self._code_generation:
                return
            if reason is None:
                self._codePairDone.emit(True, self._code_label)
            else:
                self._codePairDone.emit(False, reason)

        asyncio.run_coroutine_threadsafe(run(), loop)
        return True

    def cancel_code_pairing(self) -> None:
        """Отменить текущую попытку. Об исходе отменённой попытки сигнала не будет."""
        self._code_generation += 1
        if self._engine is None or self._engine._loop is None:
            return

        def cancel():
            future, offer, join = self._code_sas, self._code_offer, self._code_join
            if future is not None and not future.done():
                future.set_result(False)
            if offer is not None:
                offer.close()
            if join is not None:
                join.cancel()

        self._engine._loop.call_soon_threadsafe(cancel)
