"""Сопряжение двух компьютеров по короткому коду.

QR (см. pairing.py) рассчитан на телефон с камерой. Между компьютерами пользователь
переносит данные руками, поэтому здесь короткий код из 6 цифр:

  * старый ПК показывает код, слушает порт и анонсирует предложение по mDNS;
  * новый ПК находит предложение (mDNS, при неудаче — перебор своей подсети) и
    присылает введённый код;
  * оба экрана показывают контрольное число, пользователь подтверждает совпадение
    на ОБОИХ компьютерах. Только после этого устройства доверяют друг другу.

Безопасность. До сопряжения сертификаты не закреплены, поэтому TLS здесь шифрует, но
не аутентифицирует. Шести цифр недостаточно против посредника: подслушав код, он мог
бы подключиться к настоящему ПК сам. Подмену ловит контрольное число — оно зависит от
fingerprint обоих сертификатов, и у посредника на двух плечах получились бы разные.

Чтобы посредник не мог ПОДОБРАТЬ свой сертификат или случайное значение под совпадение,
клиент сначала фиксирует хеш своего значения (commit) и раскрывает его только после
значения сервера. На момент выбора ни одна сторона не знает итогового числа, поэтому
у посредника одна попытка с вероятностью 10^-6. Несовпадение закрывает предложение.

Перебор кода ограничен: не больше MAX_CODE_ATTEMPTS неверных попыток и не дольше
CODE_TTL секунд. После принятия кода предложение занято и второй раз не сработает.

Формат кадров общий с основным протоколом (wire.py), типы сообщений — `cp_*`.
Телефон этот путь не использует.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import ssl
import threading
from dataclasses import dataclass
from typing import Awaitable, Callable

from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

from . import discovery as discovery_mod
from . import identity as identity_mod
from . import pairing, peers, wire

PROTO = 1
OFFER_SERVICE_TYPE = "_qtnotes-pair._tcp.local."

# Постоянный порт: его можно открыть в файрволе и найти перебором подсети, когда mDNS
# фильтруется. Если он занят — берём любой свободный (тогда находит только mDNS).
CODE_PAIR_PORT = 41196

CODE_TTL = 180.0            # сек, сколько действует код
MAX_CODE_ATTEMPTS = 5       # неверных кодов до закрытия предложения
CONNECT_TIMEOUT = 5.0       # сек на TCP + TLS
HANDSHAKE_TIMEOUT = 15.0    # сек на одно сообщение до показа контрольного числа
CONFIRM_TIMEOUT = 180.0     # сек на подтверждение человеком
DISCOVER_TIMEOUT = 3.0      # сек на поиск предложения по mDNS
SCAN_TIMEOUT = 0.7          # сек на проверку одного адреса при переборе
SCAN_CONCURRENCY = 64       # сколько адресов проверяем одновременно

# Причины неудачи: их переводит в текст UI.
REASON_CODE = "code"            # неверный код
REASON_BUSY = "busy"            # предложение уже занято другим подключением
REASON_EXPIRED = "expired"      # код истёк
REASON_ATTEMPTS = "attempts"    # слишком много неверных кодов
REASON_REJECTED = "rejected"    # контрольное число не подтвердили
REASON_ABORTED = "aborted"      # связь оборвалась
REASON_NETWORK = "network"      # не удалось подключиться
REASON_NOT_FOUND = "not_found"  # другой ПК не найден
REASON_PAIRED = "paired"        # успех (только для on_closed)
REASON_CANCELLED = "cancelled"  # отменено пользователем

_SAS_DOMAIN = b"qtnotes/code-pairing-sas/v1"

_NET_ERRORS = (wire.ProtocolError, asyncio.IncompleteReadError, asyncio.TimeoutError,
               OSError, ValueError, TypeError)

DecideSas = Callable[[str, str], Awaitable[bool]]
OnPaired = Callable[[peers.Peer, str, int], None]


class CodePairingError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def generate_code() -> str:
    return f"{secrets.randbelow(10 ** 6):06d}"


def normalize_code(text: str) -> str:
    """Оставить только цифры: код можно ввести как `123 456`."""
    return "".join(ch for ch in text if ch.isascii() and ch.isdigit())


def format_digits(value: str) -> str:
    """`123456` → `123 456`: шесть цифр подряд труднее читать и сверять."""
    return f"{value[:3]} {value[3:]}"


def _commit(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("ascii")).hexdigest()


def compute_sas(code: str, server_fp: str, client_fp: str,
                client_nonce: str, server_nonce: str) -> str:
    """Контрольное число, которое пользователь сверяет на двух экранах."""
    h = hashlib.sha256()
    for part in (_SAS_DOMAIN, code.encode(), server_fp.encode(), client_fp.encode(),
                 client_nonce.encode(), server_nonce.encode()):
        h.update(len(part).to_bytes(4, "big"))
        h.update(part)
    return f"{int.from_bytes(h.digest()[:8], 'big') % 10 ** 6:06d}"


async def _read(reader, timeout: float) -> dict:
    kind, msg = await asyncio.wait_for(wire.read_frame(reader), timeout)
    if kind != "control" or not isinstance(msg, dict):
        raise wire.ProtocolError("ожидалось управляющее сообщение")
    return msg


def _confirmed(msg: dict) -> bool:
    return msg.get("type") == "cp_confirm" and msg.get("ok") is True


def _peer_sync_port(msg: dict) -> int:
    port = msg.get("sync_port")
    return port if isinstance(port, int) and 0 < port < 65536 else 0


async def _exchange_confirm(reader, writer, decide: Callable[[], Awaitable[bool]],
                            sync_port: int) -> dict | None:
    """Обменяться решениями людей. Вернуть сообщение пира, если согласны оба.

    Ждём одновременно и своего человека, и пира: если второй ПК отказал, этот не должен
    висеть на вопросе, ответ на который уже ничего не решает.
    """
    remote = asyncio.ensure_future(_read(reader, CONFIRM_TIMEOUT))
    local = asyncio.ensure_future(decide())
    try:
        await asyncio.wait({remote, local}, timeout=CONFIRM_TIMEOUT,
                           return_when=asyncio.FIRST_COMPLETED)
        if not remote.done() and not local.done():
            return None
        if remote.done() and not remote.exception() and not _confirmed(remote.result()):
            return None     # пир отказал: свой ответ уже ничего не решает
        if not local.done():
            remote.result()     # пир мог отключиться, не ответив
            mine = await asyncio.wait_for(local, CONFIRM_TIMEOUT)
        else:
            mine = local.result()
        try:
            await wire.write_message(writer, {"type": "cp_confirm", "ok": bool(mine),
                                              "sync_port": sync_port})
        except OSError:
            pass            # пир мог уже отказать и закрыть соединение: решит чтение ниже
        if not mine:
            return None
        msg = await remote
        return msg if _confirmed(msg) else None
    finally:
        for task in (remote, local):
            if not task.done():
                task.cancel()


# --- сторона старого ПК: показываем код и слушаем ---

class CodeOffer:
    """Запущенное предложение сопряжения. Живёт до первого исхода или `close()`."""

    def __init__(self, server, finish: Callable[[str], None]):
        self._server = server
        self._finish = finish
        self.port: int = server.sockets[0].getsockname()[1]

    def close(self) -> None:
        self._finish(REASON_CANCELLED)


async def serve_code_pairing(identity, code: str, decide_sas: DecideSas, on_paired: OnPaired,
                             on_closed: Callable[[str], None] | None = None,
                             host: str = "0.0.0.0", port: int = CODE_PAIR_PORT,
                             ttl: float = CODE_TTL, sync_port: int = 0) -> CodeOffer:
    """Запустить предложение. `on_closed(reason)` вызывается ровно один раз."""
    from . import transport

    loop = asyncio.get_running_loop()
    state = {"closed": False, "taken": False, "attempts": 0}
    holder: dict = {}

    def finish(reason: str) -> None:
        if state["closed"]:
            return
        state["closed"] = True
        holder["server"].close()
        holder["timer"].cancel()
        if on_closed is not None:
            on_closed(reason)

    def expire() -> None:
        if not state["taken"]:      # идёт сверка — её исход сам закроет предложение
            finish(REASON_EXPIRED)

    async def fail(writer, reason: str) -> None:
        await wire.write_message(writer, {"type": "pair_err", "reason": reason})

    async def negotiate(reader, writer, hello: dict, peer_id: str, cert_pem: str) -> str:
        """Всё после принятия кода. Вернуть причину закрытия предложения."""
        name = str(hello.get("name") or "")
        commit = str(hello.get("commit") or "")
        server_nonce = secrets.token_hex(32)
        await wire.write_message(writer, {
            "type": "cp_challenge", "nonce": server_nonce,
            "device_id": identity.device_id, "name": identity.name})

        reveal = await _read(reader, HANDSHAKE_TIMEOUT)
        client_nonce = reveal.get("nonce")
        if reveal.get("type") != "cp_reveal" or not isinstance(client_nonce, str) \
                or not client_nonce.isascii() \
                or not hmac.compare_digest(_commit(client_nonce), commit):
            return REASON_ABORTED

        client_fp = identity_mod.fingerprint_from_cert_pem(cert_pem.encode())
        sas = compute_sas(code, identity.fingerprint, client_fp, client_nonce, server_nonce)
        confirm = await _exchange_confirm(reader, writer, lambda: decide_sas(sas, name),
                                          sync_port)
        if confirm is None:
            return REASON_REJECTED

        peer = peers.Peer(device_id=peer_id, name=name, cert_pem=cert_pem,
                          paired_at=pairing._now())
        peername = writer.get_extra_info("peername")
        on_paired(peer, peername[0] if peername else "", _peer_sync_port(confirm))
        await wire.write_message(writer, {"type": "cp_done"})
        return REASON_PAIRED

    async def handler(reader, writer) -> None:
        took_offer = False
        reason = REASON_ABORTED
        try:
            hello = await _read(reader, HANDSHAKE_TIMEOUT)
            if hello.get("type") != "cp_hello":
                return
            if state["closed"]:
                await fail(writer, REASON_EXPIRED)
                return
            if state["taken"]:
                await fail(writer, REASON_BUSY)
                return
            if not hmac.compare_digest(str(hello.get("code") or "").encode(), code.encode()):
                state["attempts"] += 1
                await fail(writer, REASON_CODE)
                if state["attempts"] >= MAX_CODE_ATTEMPTS:
                    finish(REASON_ATTEMPTS)
                return
            cert_pem = hello.get("cert_pem")
            if not isinstance(cert_pem, str) or not cert_pem:
                await fail(writer, "cert")
                return
            peer_id = identity_mod.device_id_from_cert_pem(cert_pem.encode())
            if peer_id != hello.get("device_id") or peer_id == identity.device_id:
                await fail(writer, "cert")
                return
            # код верен — предложение занято; повторить его нельзя при любом исходе
            state["taken"] = True
            took_offer = True
            reason = await negotiate(reader, writer, hello, peer_id, cert_pem)
        except _NET_ERRORS:
            pass
        finally:
            writer.close()
            if took_offer:
                finish(reason)

    ctx = transport.make_server_context(identity.cert_path, identity.key_path, None)
    try:
        server = await asyncio.start_server(handler, host, port, ssl=ctx)
    except OSError:
        server = await asyncio.start_server(handler, host, 0, ssl=ctx)
    holder["server"] = server
    holder["timer"] = loop.call_later(ttl, expire)
    return CodeOffer(server, finish)


# --- сторона нового ПК: находим предложение и присылаем код ---

async def join_code_pairing(identity, code: str, host: str, port: int,
                            decide_sas: DecideSas, on_paired: OnPaired,
                            sync_port: int = 0) -> peers.Peer:
    """Подключиться к предложению. При отказе — CodePairingError с причиной."""
    # Цепочку не проверяем: до сопряжения закреплять нечего, подмену ловит контрольное число.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(str(identity.cert_path), str(identity.key_path))

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx), CONNECT_TIMEOUT)
    except _NET_ERRORS as e:
        raise CodePairingError(REASON_NETWORK) from e
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        if not der:
            raise CodePairingError(REASON_ABORTED)
        server_fp = hashlib.sha256(der).hexdigest()
        server_pem = ssl.DER_cert_to_PEM_cert(der)
        server_id = identity_mod.device_id_from_cert_pem(server_pem.encode())

        client_nonce = secrets.token_hex(32)
        await wire.write_message(writer, {
            "type": "cp_hello", "v": PROTO, "code": code, "commit": _commit(client_nonce),
            "device_id": identity.device_id, "name": identity.name,
            "cert_pem": identity.cert_pem.decode()})

        challenge = await _read(reader, HANDSHAKE_TIMEOUT)
        if challenge.get("type") == "pair_err":
            raise CodePairingError(str(challenge.get("reason") or REASON_ABORTED))
        server_nonce = challenge.get("nonce")
        if challenge.get("type") != "cp_challenge" or not isinstance(server_nonce, str) \
                or challenge.get("device_id") != server_id:
            raise CodePairingError(REASON_ABORTED)

        await wire.write_message(writer, {"type": "cp_reveal", "nonce": client_nonce})
        name = str(challenge.get("name") or "")
        sas = compute_sas(code, server_fp, identity.fingerprint, client_nonce, server_nonce)
        confirm = await _exchange_confirm(reader, writer, lambda: decide_sas(sas, name),
                                          sync_port)
        if confirm is None:
            raise CodePairingError(REASON_REJECTED)

        done = await _read(reader, HANDSHAKE_TIMEOUT)
        if done.get("type") != "cp_done":
            raise CodePairingError(REASON_ABORTED)
        peer = peers.Peer(device_id=server_id, name=name, cert_pem=server_pem,
                          paired_at=pairing._now())
        on_paired(peer, host, _peer_sync_port(confirm))
        return peer
    except _NET_ERRORS as e:
        raise CodePairingError(REASON_ABORTED) from e
    finally:
        writer.close()


# --- поиск предложения в сети ---

class _Announcer:
    """Анонс предложения по mDNS. Методы блокирующие — звать из отдельного потока."""

    def __init__(self):
        self._zc: Zeroconf | None = None
        self._info = None

    def start(self, identity, port: int) -> None:
        self._info = discovery_mod.build_service_info(
            identity, port, service_type=OFFER_SERVICE_TYPE)
        self._zc = Zeroconf()
        self._zc.register_service(self._info)

    def stop(self) -> None:
        zc, info = self._zc, self._info
        self._zc = self._info = None
        if zc is None:
            return
        try:
            if info is not None:
                zc.unregister_service(info)
        finally:
            zc.close()


def new_announcer() -> _Announcer:
    return _Announcer()


@dataclass(frozen=True)
class OfferAddress:
    host: str
    port: int


def discover_offers(our_device_id: str, timeout: float = DISCOVER_TIMEOUT) -> list[OfferAddress]:
    """Найти предложения по mDNS. Блокирующая — звать из отдельного потока."""
    found: dict[str, OfferAddress] = {}
    got_one = threading.Event()

    class Listener(ServiceListener):
        def add_service(self, zc, type_, name):  # noqa: N802
            parsed = discovery_mod.parse_service_info(
                zc.get_service_info(type_, name), our_device_id)
            if parsed is not None:
                found[parsed.device_id] = OfferAddress(parsed.host, parsed.port)
                got_one.set()

        def update_service(self, zc, type_, name):  # noqa: N802
            self.add_service(zc, type_, name)

        def remove_service(self, zc, type_, name):  # noqa: N802
            pass

    try:
        zc = Zeroconf()
    except OSError:
        return []
    try:
        ServiceBrowser(zc, OFFER_SERVICE_TYPE, Listener())
        if got_one.wait(timeout):
            got_one.clear()
            got_one.wait(0.3)       # вдруг в сети не одно предложение
    finally:
        zc.close()
    return list(found.values())


async def scan_offers(port: int = CODE_PAIR_PORT, hosts: list[str] | None = None,
                      timeout: float = SCAN_TIMEOUT) -> list[OfferAddress]:
    """Найти предложения перебором своей подсети: mDNS часто режут на роутерах."""
    candidates = hosts if hosts is not None else discovery_mod.scan_candidates()
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)
    found: list[OfferAddress] = []

    async def probe(host: str) -> None:
        async with sem:
            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout)
            except (OSError, asyncio.TimeoutError):
                return
            writer.close()
            found.append(OfferAddress(host, port))

    await asyncio.gather(*(probe(h) for h in candidates))
    return found
