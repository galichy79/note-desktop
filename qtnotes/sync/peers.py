"""Доверенные (сопряжённые) устройства — trust-store в peers.json.

Каждый пир: device_id, человекочитаемое имя, его cert (PEM) для TLS-pinning и
время сопряжения. Файл лежит рядом с настройками (config.peers_path()), НЕ в vault.

Здесь же — адреса пиров (peer_addrs.json): последний известный host:port каждого
пира, чтобы дозваниваться без mDNS. Доверие и адрес разведены: cert — навсегда,
адрес устаревает и обновляется сам.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .. import config
from .. import fsutil


@dataclass
class Peer:
    device_id: str
    name: str
    cert_pem: str        # PEM-строка сертификата пира (для сверки при TLS)
    paired_at: str

    def as_dict(self) -> dict:
        return {"device_id": self.device_id, "name": self.name,
                "cert_pem": self.cert_pem, "paired_at": self.paired_at}

    @classmethod
    def from_dict(cls, d: dict) -> "Peer":
        return cls(device_id=d["device_id"], name=d.get("name", ""),
                   cert_pem=d.get("cert_pem", ""), paired_at=d.get("paired_at", ""))


def _load_raw() -> list[dict]:
    p = config.peers_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_raw(items: list[dict]) -> None:
    p = config.peers_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def list_peers() -> list[Peer]:
    return [Peer.from_dict(d) for d in _load_raw()]


def get_peer(device_id: str) -> Peer | None:
    for p in list_peers():
        if p.device_id == device_id:
            return p
    return None


def add_peer(device_id: str, name: str, cert_pem: str) -> Peer:
    """Добавить/обновить доверенное устройство (по device_id)."""
    peers = [p for p in list_peers() if p.device_id != device_id]
    peer = Peer(device_id=device_id, name=name, cert_pem=cert_pem,
                paired_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    peers.append(peer)
    _save_raw([p.as_dict() for p in peers])
    return peer


def remove_peer(device_id: str) -> None:
    peers = [p for p in list_peers() if p.device_id != device_id]
    _save_raw([p.as_dict() for p in peers])
    # и адрес тоже: иначе заново сопряжённое устройство унаследует старый
    addrs = load_peer_addrs()
    if addrs.pop(device_id, None) is not None:
        _save_addrs(addrs)


def is_trusted(device_id: str) -> bool:
    return get_peer(device_id) is not None


def update_peer_name(device_id: str, name: str) -> bool:
    """Обновить имя пира — он назвался по-другому (например, по модели телефона).

    Имя не входит в cert и ни на что, кроме отображения, не влияет: доверие держится на
    device_id и cert. False — если менять нечего (пира нет, имя пустое или то же самое).
    """
    name = (name or "").strip()
    if not name:
        return False
    peers = list_peers()
    for p in peers:
        if p.device_id == device_id:
            if p.name == name:
                return False
            p.name = name
            _save_raw([x.as_dict() for x in peers])
            return True
    return False


# --- адреса пиров (host:port для прямого подключения, в обход mDNS) ---
#
# Формат тот же, что у телефона (peer_addrs.json: device_id → {host, port}), и правило
# слияния — тоже (см. merge_peer_addr, зеркало mergePeerAddr в note-mobile).

def load_peer_addrs() -> dict[str, dict]:
    p = config.peer_addrs_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_addrs(addrs: dict[str, dict]) -> None:
    config.peer_addrs_path().parent.mkdir(parents=True, exist_ok=True)
    fsutil.atomic_write_bytes(
        config.peer_addrs_path(),
        json.dumps(addrs, ensure_ascii=False, indent=2).encode("utf-8"))


def merge_peer_addr(current: dict | None, host: str, port: int) -> dict | None:
    """Адрес пира для прямого подключения: `{'host': ..., 'port': ...}`.

    Зовётся регулярно (живая сессия, mDNS), поэтому возвращаем **None**, когда
    писать нечего: адрес непригоден или уже совпадает с сохранённым.

    `port` = 0 означает «пир не сообщил свой порт» (старая версия на той стороне) —
    тогда держим уже сохранённый: хост мог смениться, а порт прослушивания у пира
    стабильный (config.sync_port). Без порта адрес бесполезен — запись не создаём.
    """
    if not host:
        return None
    known = int((current or {}).get("port", 0) or 0)
    effective = port if port > 0 else known
    if effective <= 0:
        return None
    if current is not None and current.get("host") == host and known == effective:
        return None  # не изменился — файл не переписываем
    return {"host": host, "port": effective}


def save_peer_addr(device_id: str, host: str, port: int) -> bool:
    """Запомнить адрес пира. False — если записывать нечего (см. merge_peer_addr)."""
    addrs = load_peer_addrs()
    nxt = merge_peer_addr(addrs.get(device_id), host, port)
    if nxt is None:
        return False
    addrs[device_id] = nxt
    _save_addrs(addrs)
    return True
