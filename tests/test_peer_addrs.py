"""Адрес пира для прямого подключения (десктоп): запоминание и дозвон без mDNS.

Повод: телефон раздаёт интернет, ПК — его клиент. mDNS там не работает (у интерфейса
точки доступа нет сети в ConnectivityManager, NsdManager остаётся на мобильной сети
без мультикаста), поэтому пара держится только на сохранённом адресе. Значит, адрес
должен сам освежаться из живой сессии, а десктоп — уметь дозвониться по нему сам.

Проверяем на реальном TLS-loopback:
  1) правила слияния адреса (merge_peer_addr) — зеркало mergePeerAddr в note-mobile;
  2) принимающая сторона узнаёт адрес того, кто дозвонился, и порт берёт из hello,
     а не исходный (у входящего соединения он эфемерный);
  3) сохранённый адрес поднимает сессию сам, без mDNS;
  4) remove_peer убирает и адрес.

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_peer_addrs.py
"""

import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_merge_rules():
    from qtnotes.sync.peers import merge_peer_addr as m
    assert m(None, "192.168.43.5", 8765) == {"host": "192.168.43.5", "port": 8765}
    # IP сменился, пир порт не сообщил (старая версия) → держим сохранённый
    assert m({"host": "192.168.43.5", "port": 8765}, "192.168.43.9", 0) == \
        {"host": "192.168.43.9", "port": 8765}
    # пир сообщил новый порт → обновляем и порт
    assert m({"host": "192.168.43.5", "port": 8765}, "192.168.43.5", 9000) == \
        {"host": "192.168.43.5", "port": 9000}
    # не изменился → None (файл не переписываем)
    assert m({"host": "192.168.43.5", "port": 8765}, "192.168.43.5", 8765) is None
    assert m({"host": "192.168.43.5", "port": 8765}, "192.168.43.5", 0) is None
    # порт неизвестен и запоминать нечего
    assert m(None, "192.168.43.5", 0) is None
    assert m(None, "", 8765) is None
    print("OK: правила слияния адреса")


def test_store(tmp_cfg):
    from qtnotes import config
    from qtnotes.sync import peers
    os.environ["XDG_CONFIG_HOME"] = tmp_cfg

    assert peers.save_peer_addr("dev1", "192.168.43.5", 8765) is True
    assert peers.load_peer_addrs() == {"dev1": {"host": "192.168.43.5", "port": 8765}}
    # тот же адрес повторно — не пишем (иначе файл переписывался бы каждые 20 с)
    assert peers.save_peer_addr("dev1", "192.168.43.5", 8765) is False
    # хост сменился, порт не сообщён — порт на месте
    assert peers.save_peer_addr("dev1", "192.168.43.9", 0) is True
    assert peers.load_peer_addrs()["dev1"] == {"host": "192.168.43.9", "port": 8765}
    assert config.peer_addrs_path().exists()

    # remove_peer чистит и адрес
    peers.add_peer("dev1", "Телефон", "PEM")
    peers.remove_peer("dev1")
    assert "dev1" not in peers.load_peer_addrs(), "адрес удалённого пира остался"
    print("OK: хранилище адресов + очистка при remove_peer")


def test_peer_name(tmp_cfg):
    """Имя пира обновляется из hello: иначе два телефона так и останутся «Телефон»."""
    from qtnotes.sync import peers
    os.environ["XDG_CONFIG_HOME"] = tmp_cfg

    assert peers.update_peer_name("nosuch", "X") is False, "несуществующего пира трогать нечем"
    peers.add_peer("dev9", "Телефон", "PEM")
    assert peers.update_peer_name("dev9", "Телефон SM-G970F") is True
    assert peers.get_peer("dev9").name == "Телефон SM-G970F"
    # то же имя и пустое имя — не пишем (файл не переписываем на каждом hello)
    assert peers.update_peer_name("dev9", "Телефон SM-G970F") is False
    assert peers.update_peer_name("dev9", "   ") is False
    assert peers.get_peer("dev9").name == "Телефон SM-G970F"
    peers.remove_peer("dev9")
    print("OK: имя пира освежается из сессии, пустое/неизменившееся — нет")


async def scenario(idA, idB, store, cfg):
    """Реальный TLS-loopback: телефон (A) дозвонился, десктоп (B) — нет."""
    from qtnotes.sync import engine as engine_mod
    from qtnotes.sync import peers as peers_mod

    # cert_pem у Identity — bytes; в trust-store (и на проводе) он строкой
    peer_a = peers_mod.Peer(idA.device_id, "Телефон", idA.cert_pem.decode(), "now")
    peer_b = peers_mod.Peer(idB.device_id, "Десктоп", idB.cert_pem.decode(), "now")
    # оба движка пишут в один peer_addrs.json (в тесте это один процесс; в бою —
    # отдельный файл на устройство), ключи разные — записи не мешают друг другу
    os.environ["XDG_CONFIG_HOME"] = cfg

    eng_a = engine_mod.SyncEngine(idA, store, get_peers=lambda: [peer_b])
    eng_b = engine_mod.SyncEngine(idB, store, get_peers=lambda: [peer_a])
    await eng_a.serve(host="127.0.0.1", port=0)
    await eng_b.serve(host="127.0.0.1", port=0)

    # --- A дозванивается (как телефон из QR) ---
    # сессия ключуется id ПИРА: у B это A и наоборот
    dial = asyncio.ensure_future(eng_a.connect("127.0.0.1", eng_b._port, idB.device_id))
    for _ in range(50):
        await asyncio.sleep(0.1)
        if idA.device_id in eng_b.sessions:
            break
        if dial.done():
            print(f"дозвон завершился: {dial.exception()!r}")
            break
    assert idA.device_id in eng_b.sessions, "сессия не поднялась"
    assert idB.device_id in eng_a.sessions, "сессия не поднялась у инициатора"

    addrs = peers_mod.load_peer_addrs()
    assert addrs.get(idA.device_id) == {"host": "127.0.0.1", "port": eng_a._port}, \
        f"B не узнал адрес A: {addrs.get(idA.device_id)} (порт из hello = {eng_a._port})"
    assert addrs.get(idB.device_id) == {"host": "127.0.0.1", "port": eng_b._port}, \
        f"A не узнал адрес B: {addrs.get(idB.device_id)}"
    print(f"OK: обе стороны узнали адрес пира из сессии "
          f"({eng_a._port} и {eng_b._port} — порты прослушивания из hello)")

    # --- сессия оборвалась: B должен подняться сам по сохранённому адресу ---
    dial.cancel()
    for _ in range(50):
        await asyncio.sleep(0.1)
        if not eng_b.sessions and not eng_a.sessions:
            break
    assert not eng_b.sessions, "сессия не закрылась"

    engine_mod.RECONNECT_INTERVAL = 0.2  # не ждать 20 с в тесте
    loop_task = asyncio.ensure_future(eng_b._reconnect_loop())
    dialed = False
    for _ in range(60):
        await asyncio.sleep(0.1)
        if idA.device_id in eng_b.sessions:
            dialed = True
            break
    assert dialed, "B не дозвонился по сохранённому адресу (mDNS здесь молчит вовсе)"
    print("OK: дозвон по сохранённому адресу поднял сессию без mDNS")

    loop_task.cancel()
    for e in (eng_a, eng_b):
        for s in list(e.sessions.values()):
            s.close()
        if e._server is not None:
            e._server.close()
            await e._server.wait_closed()


def main():
    from qtnotes import config
    from qtnotes.sync import identity, store as store_mod

    base = tempfile.mkdtemp(prefix="qtnotes-addr-")
    cfg_a = os.path.join(base, "cfgA")   # «телефон»
    cfg_b = os.path.join(base, "cfgB")   # «десктоп»
    vault = os.path.join(base, "vault")
    for d in (cfg_a, cfg_b, vault):
        os.makedirs(d)

    try:
        test_merge_rules()
        test_store(cfg_b)
        test_peer_name(cfg_b)

        os.environ["XDG_CONFIG_HOME"] = cfg_a
        id_a = identity.ensure_identity()
        os.environ["XDG_CONFIG_HOME"] = cfg_b
        id_b = identity.ensure_identity()
        assert id_a.device_id != id_b.device_id

        os.environ["QTNOTES_VAULT"] = vault
        os.environ["XDG_CONFIG_HOME"] = cfg_b
        store = store_mod.GlobalStore()

        asyncio.run(scenario(id_a, id_b, store, cfg_b))
        print("ALL PEER ADDR TESTS PASSED")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
