"""事实解析投影、实体聚合反向索引与重键事件。

原始事实始终一行不动，:class:`ResolutionSnapshot` 只是按某一图版本计算的
投影：每条事实带原始 ``source_alias``（血缘）与当时解析到的 ``entity_key``。

身份声明变化时按旧/新分量闭包撤销旧实体汇总、加入新实体汇总，
并生成 :class:`RekeyEvent`。重键只移动实体标签：受影响旧实体撤销的总量
恒等于新实体记入的总量（金额守恒），不产生任何新增用量，也不按相同金额
把独立事实去重。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .fact_ledger import Fact, FactLedger
from .identity_graph import GraphSnapshot, IdentityGraph, affected_closure


class ResolutionError(RuntimeError):
    """投影与账本不变量被破坏（例如金额不守恒）。"""


@dataclass(frozen=True)
class ProjectedFact:
    """一条事实在某图版本下的投影，原始来源身份完整保留。"""

    fact_id: str
    source_alias: str
    entity_key: str
    usage: int


@dataclass(frozen=True)
class EntityAggregate:
    """单个解析实体的汇总。"""

    entity_key: str
    aliases: tuple[str, ...]
    fact_ids: tuple[str, ...]
    total_usage: int
    usage_by_fact: Mapping[str, int]


@dataclass(frozen=True)
class ResolutionSnapshot:
    """图与聚合在同一版本下联合发布的一致快照。"""

    version: int
    projected_facts: tuple[ProjectedFact, ...]
    entities: Mapping[str, EntityAggregate]
    # 聚合反向索引：实体 -> 事实 / 事实 -> 实体 / 原 alias -> 实体
    facts_by_entity: Mapping[str, tuple[str, ...]]
    entity_by_fact: Mapping[str, str]
    entity_by_alias: Mapping[str, str]
    grand_total_usage: int


@dataclass(frozen=True)
class RekeyEvent:
    """一次身份图版本变化造成的汇总重键（标签移动，不是新增用量）。"""

    version_from: int
    version_to: int
    changed_aliases: tuple[str, ...]
    old_keys: tuple[str, ...]
    new_keys: tuple[str, ...]
    withdrawn_entities: tuple[EntityAggregate, ...]
    added_entities: tuple[EntityAggregate, ...]
    moved_usage: int

    @property
    def usage_conserved(self) -> bool:
        return (
            sum(entity.total_usage for entity in self.withdrawn_entities)
            == sum(entity.total_usage for entity in self.added_entities)
            == self.moved_usage
        )


@dataclass
class ResolutionStore:
    """为图的每个版本生成一致投影与聚合，并给出跨版本重键事件。"""

    ledger: FactLedger
    graph: IdentityGraph
    _snapshots: list[ResolutionSnapshot] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self._snapshots:
            self._snapshots = [
                self._build_snapshot(self.graph.snapshot(0))
            ]

    @property
    def snapshots(self) -> tuple[ResolutionSnapshot, ...]:
        return tuple(self._snapshots)

    def rebuild(self) -> None:
        """图追加新版本后重算投影；图与聚合必须成对更新。"""
        existing = len(self._snapshots)
        for graph_snapshot in self.graph.versions[existing:]:
            self._snapshots.append(self._build_snapshot(graph_snapshot))

    def snapshot(self, version: int) -> ResolutionSnapshot:
        return self._snapshots[version]

    @property
    def current(self) -> ResolutionSnapshot:
        return self._snapshots[-1]

    def rekey_events(self) -> tuple[RekeyEvent, ...]:
        """相邻图版本之间的全部重键事件（含空变化检查与守恒断言）。"""
        events: list[RekeyEvent] = []
        for new_snapshot in self._snapshots[1:]:
            version = new_snapshot.version
            old_snapshot = self._snapshots[version - 1]
            closure = affected_closure(
                self.graph.snapshot(version - 1),
                self.graph.snapshot(version),
            )
            withdrawn = tuple(
                old_snapshot.entities[key]
                for key in closure.old_keys
                if key in old_snapshot.entities
            )
            added = tuple(
                new_snapshot.entities[key]
                for key in closure.new_keys
                if key in new_snapshot.entities
            )
            moved = sum(entity.total_usage for entity in withdrawn)
            event = RekeyEvent(
                version_from=version - 1,
                version_to=version,
                changed_aliases=closure.changed_aliases,
                old_keys=closure.old_keys,
                new_keys=closure.new_keys,
                withdrawn_entities=withdrawn,
                added_entities=added,
                moved_usage=moved,
            )
            if not event.usage_conserved:
                raise ResolutionError(
                    f"版本 {version - 1}->{version} 重键金额不守恒: "
                    f"撤销 {moved} != 记入 "
                    f"{sum(entity.total_usage for entity in added)}"
                )
            events.append(event)
        return tuple(events)

    def _build_snapshot(
        self, graph_snapshot: GraphSnapshot
    ) -> ResolutionSnapshot:
        projected: list[ProjectedFact] = []
        facts_by_entity: dict[str, list[str]] = {}
        entity_by_fact: dict[str, str] = {}
        entity_by_alias: dict[str, str] = {}

        for fact in self.ledger.facts:
            entity_key = graph_snapshot.entity_of(fact.source_alias)
            projected.append(
                ProjectedFact(
                    fact_id=fact.fact_id,
                    source_alias=fact.source_alias,
                    entity_key=entity_key,
                    usage=fact.usage,
                )
            )
            facts_by_entity.setdefault(entity_key, []).append(fact.fact_id)
            entity_by_fact[fact.fact_id] = entity_key
            entity_by_alias[fact.source_alias] = entity_key

        # 图中存在但从未承载事实的 alias 也要进入别名血缘索引，
        # 其实体汇总为零用量。
        for alias, canonical in graph_snapshot.canonical.items():
            entity_by_alias.setdefault(alias, canonical)

        entities: dict[str, EntityAggregate] = {}
        for component in graph_snapshot.components:
            key = min(component)
            member_facts = sorted(
                fact.fact_id
                for fact in self.ledger.facts
                if graph_snapshot.entity_of(fact.source_alias) == key
            )
            usage_by_fact = {
                fact_id: self.ledger.get(fact_id).usage
                for fact_id in member_facts
            }
            entities[key] = EntityAggregate(
                entity_key=key,
                aliases=tuple(sorted(component)),
                fact_ids=tuple(member_facts),
                total_usage=sum(usage_by_fact.values()),
                usage_by_fact=usage_by_fact,
            )
        # 有事实但图中没有任何声明的单点 alias。
        for entity_key, fact_ids in facts_by_entity.items():
            if entity_key not in entities:
                entities[entity_key] = EntityAggregate(
                    entity_key=entity_key,
                    aliases=(entity_key,),
                    fact_ids=tuple(fact_ids),
                    total_usage=sum(
                        self.ledger.get(fact_id).usage for fact_id in fact_ids
                    ),
                    usage_by_fact={
                        fact_id: self.ledger.get(fact_id).usage
                        for fact_id in fact_ids
                    },
                )

        grand_total = sum(fact.usage for fact in self.ledger.facts)
        aggregated_total = sum(
            entity.total_usage for entity in entities.values()
        )
        if grand_total != aggregated_total:
            raise ResolutionError(
                f"版本 {graph_snapshot.version} 聚合总额 {aggregated_total} "
                f"与事实总额 {grand_total} 不一致"
            )
        if len(projected) != len(self.ledger.facts):
            raise ResolutionError("投影事实数与账本事实行数不一致")

        return ResolutionSnapshot(
            version=graph_snapshot.version,
            projected_facts=tuple(projected),
            entities=dict(sorted(entities.items())),
            facts_by_entity={
                key: tuple(sorted(value))
                for key, value in sorted(facts_by_entity.items())
            },
            entity_by_fact=dict(sorted(entity_by_fact.items())),
            entity_by_alias=dict(sorted(entity_by_alias.items())),
            grand_total_usage=grand_total,
        )
