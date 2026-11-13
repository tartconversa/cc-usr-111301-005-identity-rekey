"""不可变原始事实账本。

事实保持输入的一行不变：``fact_id`` 与 ``source_alias`` 是原始来源身份，
任何身份解析过程都不覆盖它们。
``fact_semantics`` 为不可变多重集合（immutable_multiset）：

* 同一 ``fact_id`` 原样重送是幂等的；
* 同一 ``fact_id`` 内容不同（source_alias 或用量被改）属于平级内容冲突，
  抛出 :class:`FactConflict`，事实修订不会被静默吞掉；
* 合并两条独立事实（即使金额或用量相同）绝不按业务金额去重。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class FactConflict(ValueError):
    """同一 fact_id 的平级内容冲突，拒绝覆盖原始事实。"""


@dataclass(frozen=True)
class Fact:
    """一条原始事实的不可变记录。"""

    fact_id: str
    source_alias: str
    usage: int

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Fact":
        try:
            fact_id = str(row["fact_id"])
            source_alias = str(row["source_alias"])
            raw_usage = row["usage"]
        except KeyError as exc:
            raise FactConflict(f"事实缺少必需列: {exc.args[0]}") from exc
        if not fact_id or not source_alias:
            raise FactConflict(f"事实 id/source_alias 不能为空: {row!r}")
        # 空白与 0 是两种语义，空白直接拒绝而不是当成 0。
        if raw_usage is None or str(raw_usage).strip() == "":
            raise FactConflict(f"事实 {fact_id} 用量为空，不能按 0 处理")
        try:
            usage = int(raw_usage)
        except (TypeError, ValueError) as exc:
            raise FactConflict(f"事实 {fact_id} 用量不是整数: {raw_usage!r}") from exc
        if usage < 0:
            raise FactConflict(f"事实 {fact_id} 用量不能为负: {usage}")
        return cls(fact_id=fact_id, source_alias=source_alias, usage=usage)


class FactLedger:
    """只追加、不覆盖来源身份的事实账本。"""

    def __init__(self) -> None:
        self._facts: dict[str, Fact] = {}
        self._order: list[str] = []
        self.resends = 0
        self.rejected_conflicts = 0

    @property
    def facts(self) -> tuple[Fact, ...]:
        return tuple(self._facts[fact_id] for fact_id in self._order)

    def add(self, row: Mapping[str, Any]) -> Fact:
        fact = Fact.from_row(row)
        previous = self._facts.get(fact.fact_id)
        if previous is not None:
            if previous == fact:
                self.resends += 1
                return previous
            self.rejected_conflicts += 1
            raise FactConflict(
                f"事实 {fact.fact_id} 已存在且内容不同 "
                f"(既有 {previous} vs 新送 {fact})，平级内容冲突拒绝"
            )
        self._facts[fact.fact_id] = fact
        self._order.append(fact.fact_id)
        return fact

    def load_rows(self, rows: list[Mapping[str, Any]]) -> tuple[Fact, ...]:
        return tuple(self.add(row) for row in rows)

    def get(self, fact_id: str) -> Fact:
        return self._facts[fact_id]

    def total_usage(self) -> int:
        return sum(fact.usage for fact in self.facts)
