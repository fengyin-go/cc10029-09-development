"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

除业务模块表之外，调度批次清单、待下发任务清单这类派生台账放在独立 ledger 里，
不参与模块统计。``transaction()`` 提供一次原子提交：事务期间的读写都落在一份
深拷贝副本上，只有全部写入成功才整体替换；任何一步抛错都恢复原快照，读取方
不会看到半个事务的数据。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_LEDGERS, SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._ledgers: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_LEDGERS.items()
        }
        self._lock = threading.RLock()
        self._in_transaction = False

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        with self._lock:
            return self._tables.setdefault(module, [])

    def ledger(self, name: str) -> list[dict[str, Any]]:
        with self._lock:
            return self._ledgers.setdefault(name, [])

    def append(self, name: str, row: dict[str, Any]) -> None:
        """向业务表或派生台账追加一行；事务期间写副本，提交后才可见。"""
        with self._lock:
            if name in self._tables:
                self._tables[name].append(row)
            else:
                self._ledgers.setdefault(name, []).append(row)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """原子提交：全部成功才替换表数据，异常时恢复快照。

        嵌套调用复用最外层事务，避免内层提前提交破坏原子性。事务在锁内完成，
        其他线程的台账读取会等待提交，因此不会读到半份批次或半份任务清单。
        """
        with self._lock:
            if self._in_transaction:
                yield
                return
            saved_tables = self._tables
            saved_ledgers = self._ledgers
            self._tables = copy.deepcopy(saved_tables)
            self._ledgers = copy.deepcopy(saved_ledgers)
            self._in_transaction = True
            try:
                yield
            except BaseException:
                self._tables = saved_tables
                self._ledgers = saved_ledgers
                raise
            finally:
                self._in_transaction = False

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
