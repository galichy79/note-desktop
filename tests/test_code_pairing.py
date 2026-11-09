"""Сопряжение компьютеров по коду: протокол на реальном TLS-loopback.

Проверяем:
  1) успех: оба занесли друг друга, контрольные числа равны, адреса синка переданы;
  2) неверный код отклонён, а после MAX_CODE_ATTEMPTS предложение закрыто;
  3) «числа разные» на любой стороне: доверия нет ни у кого, код больше не работает;
  4) истёкший код отклонён;
  5) посредник: контрольные числа на двух плечах не совпадают;
  6) поиск перебором находит слушателя и пропускает закрытый порт.

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_code_pairing.py
"""

import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CODE = "123456"


def main():
    from qtnotes.sync import code_pairing as cp
    from qtnotes.sync import identity

    base = tempfile.mkdtemp(prefix="qtnotes-code-")
    try:
        ids = []
        for name in ("cfgOld", "cfgNew", "cfgMitm"):
            os.environ["XDG_CONFIG_HOME"] = os.path.join(base, name)
            os.makedirs(os.environ["XDG_CONFIG_HOME"])
            ids.append(identity.ensure_identity())
        id_old, id_new, id_mitm = ids
        assert len({i.device_id for i in ids}) == 3

        class Side:
            """Одна сторона: что показали человеку, что он ответил, чему поверили."""

            def __init__(self, answer=True):
                self.answer = answer
                self.sas = []
                self.paired = []

            async def decide(self, sas, name):
                self.sas.append(sas)
                return self.answer

            def on_paired(self, peer, host, sync_port):
                self.paired.append((peer, host, sync_port))

        async def offer(side, closed, code=CODE, ttl=30.0):
            return await cp.serve_code_pairing(
                id_old, code, side.decide, side.on_paired, on_closed=closed.append,
                host="127.0.0.1", port=0, ttl=ttl, sync_port=5001)

        async def join(side, port, code=CODE, identity_=id_new):
            return await cp.join_code_pairing(
                identity_, code, "127.0.0.1", port, side.decide, side.on_paired, sync_port=5002)

        async def expect_reason(awaitable, *reasons):
            try:
                await awaitable
            except cp.CodePairingError as e:
                assert e.reason in reasons, f"{e.reason} не из {reasons}"
                return
            raise AssertionError(f"ожидался отказ {reasons}")

        async def scenario():
            # --- 1) успех ---
            old, new, closed = Side(), Side(), []
            o = await offer(old, closed)
            peer = await join(new, o.port)
            await asyncio.sleep(0.05)
            assert peer.device_id == id_old.device_id
            assert old.sas and old.sas == new.sas, (old.sas, new.sas)
            assert len(old.sas[0]) == 6 and old.sas[0].isdigit()
            got, host, port = old.paired[0]
            assert got.device_id == id_new.device_id and port == 5002 and host == "127.0.0.1"
            got, host, port = new.paired[0]
            assert got.device_id == id_old.device_id and port == 5001 and host == "127.0.0.1"
            assert got.cert_pem.strip() == id_old.cert_pem.decode().strip()
            assert closed == [cp.REASON_PAIRED], closed
            print("OK: сопряжение прошло, числа равны, адреса переданы")

            # повтор тем же кодом: слушатель уже закрыт
            await expect_reason(join(Side(), o.port), cp.REASON_NETWORK, cp.REASON_ABORTED)
            print("OK: повтор тем же кодом не проходит")

            # --- 2) неверный код и лимит попыток ---
            old, closed = Side(), []
            o = await offer(old, closed)
            for _ in range(cp.MAX_CODE_ATTEMPTS):
                new = Side()
                await expect_reason(join(new, o.port, code="000000"), cp.REASON_CODE)
                assert not new.sas and not new.paired
            await asyncio.sleep(0.05)
            assert closed == [cp.REASON_ATTEMPTS], closed
            await expect_reason(join(Side(), o.port), cp.REASON_NETWORK, cp.REASON_ABORTED,
                                cp.REASON_EXPIRED)
            assert not old.paired and not old.sas
            print("OK: неверный код отклонён, после лимита предложение закрыто")

            # --- 3) «числа разные» на каждой из сторон ---
            for old_answer, new_answer in ((False, True), (True, False), (False, False)):
                old, new, closed = Side(old_answer), Side(new_answer), []
                o = await offer(old, closed)
                await expect_reason(join(new, o.port), cp.REASON_REJECTED, cp.REASON_ABORTED)
                await asyncio.sleep(0.05)
                assert not old.paired and not new.paired, (old_answer, new_answer)
                assert closed == [cp.REASON_REJECTED], closed
                await expect_reason(join(Side(), o.port), cp.REASON_NETWORK, cp.REASON_ABORTED,
                                    cp.REASON_EXPIRED)
            print("OK: при несовпадении доверия нет, код больше не работает")

            # --- 4) истёкший код ---
            old, closed = Side(), []
            o = await offer(old, closed, ttl=0.0)
            await asyncio.sleep(0.05)
            await expect_reason(join(Side(), o.port), cp.REASON_NETWORK, cp.REASON_ABORTED,
                                cp.REASON_EXPIRED)
            assert closed == [cp.REASON_EXPIRED], closed
            print("OK: истёкший код отклонён")

            # --- 5) посредник с верным кодом: у двух плеч разные числа ---
            old, closed = Side(answer=False), []
            o = await offer(old, closed)
            mitm_client = Side(answer=False)
            await expect_reason(join(mitm_client, o.port, identity_=id_mitm),
                                cp.REASON_REJECTED, cp.REASON_ABORTED)

            async def serve_as_mitm():
                return await cp.serve_code_pairing(
                    id_mitm, CODE, mitm_server.decide, mitm_server.on_paired,
                    host="127.0.0.1", port=0, ttl=30.0)

            mitm_server, victim = Side(answer=False), Side(answer=False)
            fake = await serve_as_mitm()
            await expect_reason(join(victim, fake.port), cp.REASON_REJECTED, cp.REASON_ABORTED)
            assert old.sas != victim.sas, (old.sas, victim.sas)
            print("OK: посредник получает разные контрольные числа на двух плечах")

            # --- 6) поиск перебором ---
            old, closed = Side(), []
            o = await offer(old, closed)
            found = await cp.scan_offers(o.port, hosts=["127.0.0.1"])
            assert found == [cp.OfferAddress("127.0.0.1", o.port)], found
            o.close()
            await asyncio.sleep(0.05)
            assert await cp.scan_offers(o.port, hosts=["127.0.0.1"]) == []
            assert closed == [cp.REASON_CANCELLED], closed
            print("OK: перебор находит открытый порт и пропускает закрытый")

        asyncio.run(scenario())

        assert cp.normalize_code("123 456") == "123456"
        assert cp.format_digits("123456") == "123 456"
        a = cp.compute_sas("1", "s", "c", "n1", "n2")
        assert a == cp.compute_sas("1", "s", "c", "n1", "n2")
        assert a != cp.compute_sas("1", "x", "c", "n1", "n2")
        # поля разделены длиной: перенос символа между полями меняет число
        assert cp.compute_sas("1", "ab", "c", "n", "m") != cp.compute_sas("1", "a", "bc", "n", "m")
        print("ALL CODE PAIRING TESTS PASSED")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
