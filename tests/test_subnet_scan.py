"""Перебор своей подсети в поиске пира (десктоп).

Повод: Android умеет рандомизировать подсеть точки доступа. Тогда адреса устаревают
разом с двух сторон — телефон не может дозвониться до десктопа, десктоп до телефона, —
и вытащить пару может только перебор: кто-то должен найти второго, не зная его адреса.
Пир опознаётся по своему сертификату (пин), а не по «порт открыт»: чужой сервер на том
же порту пиром не считается.

Проверяем:
  1) кандидатов для перебора (свои адреса исключены, широкая сеть сводится к своей /24);
  2) поиск по списку хостов: пир находится по пину, чужой сервер — нет;
  3) полный путь _scan_subnets: сохранённый порт + перебор → сессия поднимается сама.

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_subnet_scan.py
"""

import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_candidates():
    from qtnotes.sync import discovery as d

    real = d.scan_candidates()
    assert real, "перебор своей подсети дал пустой список"
    mine = {ip for ip, _ in d.local_ipv4()}
    assert not (mine & set(real)), "в кандидаты попали свои же адреса"
    assert len(real) == len(set(real)), "в кандидатах есть дубликаты"
    for ip in real:                      # все строки — валидные IPv4
        parts = ip.split(".")
        assert len(parts) == 4 and all(0 <= int(p) <= 255 for p in parts), ip
    primary24 = ".".join(d._primary_ip().split(".")[:3]) + "."
    assert real[0].startswith(primary24), "подсеть основного интерфейса должна идти первой"

    # широкая сеть (мост Docker/libvirt — /16) не перебирается целиком, только своя /24
    saved = (d.local_ipv4, d._primary_ip)
    d.local_ipv4 = lambda: [("10.7.7.7", "255.255.0.0")]
    d._primary_ip = lambda: "10.7.7.7"
    try:
        narrow = d.scan_candidates()
    finally:
        d.local_ipv4, d._primary_ip = saved
    assert len(narrow) == 253, f"ожидали свою /24 (253 хоста), получили {len(narrow)}"
    assert all(ip.startswith("10.7.7.") for ip in narrow), "вышли за пределы своей /24"
    assert "10.7.7.7" not in narrow, "свой адрес не исключён"
    print("OK: кандидаты перебора (свои исключены, широкая сеть → своя /24)")


async def scenario(id_phone, id_stranger, id_desk, store):
    from qtnotes.sync import discovery as discovery_mod
    from qtnotes.sync import engine as engine_mod
    from qtnotes.sync import peers as peers_mod

    peer_of = {}
    for i in (id_phone, id_stranger, id_desk):
        peer_of[i.device_id] = peers_mod.Peer(i.device_id, i.device_id[:4],
                                              i.cert_pem.decode(), "now")
    peer_phone = peer_of[id_phone.device_id]
    peer_desk = peer_of[id_desk.device_id]

    phone = engine_mod.SyncEngine(id_phone, store, get_peers=lambda: [peer_desk])
    stranger = engine_mod.SyncEngine(id_stranger, store, get_peers=lambda: [peer_desk])
    desk = engine_mod.SyncEngine(id_desk, store, get_peers=lambda: [peer_phone])
    await phone.serve(host="127.0.0.1", port=0)
    await stranger.serve(host="127.0.0.1", port=0)
    phone_pem = id_phone.cert_pem.decode()

    # --- 1) пир находится перебором (в тесте «подсеть» — это loopback) ---
    found = await desk._scan_for_peer(id_phone.device_id, phone._port, phone_pem,
                                      hosts=["127.0.0.1"])
    assert found is not None, "пир не найден перебором"
    host, (_reader, writer) = found
    assert host == "127.0.0.1", host
    writer.close()
    print("OK: пир найден перебором и опознан по сертификату")

    # --- 2) чужой сервер на том же порту за пира не принимается ---
    miss = await desk._scan_for_peer(id_phone.device_id, stranger._port, phone_pem,
                                     hosts=["127.0.0.1"])
    assert miss is None, "чужой сервер принят за пира (пин обязан отсечь)"
    print("OK: чужой сервер отсечён пином")

    # --- 3) полный путь: сохранённый (устаревший) адрес + перебор → сессия ---
    assert peers_mod.save_peer_addr(id_phone.device_id, "192.0.2.1", phone._port), \
        "не сохранился адрес пира для проверки перебора"
    saved_candidates = discovery_mod.scan_candidates
    discovery_mod.scan_candidates = lambda: ["127.0.0.1"]   # в тесте перебираем loopback
    try:
        await desk._scan_subnets()
    finally:
        discovery_mod.scan_candidates = saved_candidates
    for _ in range(50):
        await asyncio.sleep(0.1)
        if id_phone.device_id in desk.sessions:
            break
    assert id_phone.device_id in desk.sessions, \
        "перебор не поднял сессию (мёртвый сохранённый адрес не должен мешать)"
    print("OK: перебор подсети поднял сессию в обход устаревшего адреса")

    for e in (phone, stranger, desk):
        for s in list(e.sessions.values()):
            s.close()
        if e._server is not None:
            e._server.close()
            await e._server.wait_closed()


def main():
    from qtnotes.sync import identity, store as store_mod

    base = tempfile.mkdtemp(prefix="qtnotes-scan-")
    cfgs = {name: os.path.join(base, name) for name in ("phone", "stranger", "desk")}
    vault = os.path.join(base, "vault")
    for d in (*cfgs.values(), vault):
        os.makedirs(d)

    try:
        test_candidates()

        ids = {}
        for name, cfg in cfgs.items():
            os.environ["XDG_CONFIG_HOME"] = cfg
            ids[name] = identity.ensure_identity()
        assert len({i.device_id for i in ids.values()}) == 3, "устройства не разошлись"

        os.environ["QTNOTES_VAULT"] = vault
        os.environ["XDG_CONFIG_HOME"] = cfgs["desk"]
        store = store_mod.GlobalStore()
        asyncio.run(scenario(ids["phone"], ids["stranger"], ids["desk"], store))
        print("ALL SUBNET SCAN TESTS PASSED")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
