"""版本化身份等同图。

身份声明（assertion）是带稳定 ``assertion_id`` 的无向 alias 等同边，
``revision`` 单调递增，可更正端点或撤回（``withdrawn=true``）。

规则：
* 每个被接受的新修订生成一个不可变 :class:`GraphSnapshot` 图版本；
  同一记录原样重送是空操作，不产生新版本。
* 当前有效声明边的连通分量即解析实体；``canonical_key`` 取分量内字典序
  最小 alias；没有任何声明的 alias 是单点分量（实体即自身）。
* 自环允许但不改变分量。撤回桥边会按实际连通性拆分分量，因此这里不使用
  只支持合并的并查集，而是每个版本从有效边重算连通分量。
* 同一 ``assertion_id`` 同一 ``revision`` 内容不一致属于平级内容冲突，
  过期修订同样拒绝，抛出 :class:`IdentityConflict`。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Mapping


class IdentityConflict(ValueError):
    """声明平级冲突或修订过期，拒绝接受。"""


@dataclass(frozen=True)
class Assertion:
    """一条无向等同声明的不可变记录。"""

    assertion_id: str
    revision: int
    left: str
    right: str
    withdrawn: bool

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Assertion":
        try:
            return cls(
                assertion_id=str(row["assertion_id"]),
                revision=int(row["revision"]),
                left=str(row["left"]),
                right=str(row["right"]),
                withdrawn=_as_bool(row["withdrawn"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise IdentityConflict(f"声明字段非法: {row!r}") from exc

    def __post_init__(self) -> None:
        if not self.assertion_id:
            raise IdentityConflict("assertion_id 不能为空")
        if self.revision < 1:
            raise IdentityConflict("revision 必须从 1 开始且单调递增")
        if not self.left or not self.right:
            raise IdentityConflict("声明端点 alias 不能为空")

    @property
    def edge(self) -> tuple[str, str]:
        """规范化为无向边（端点字典序），自环返回 ``(x, x)``。"""
        return tuple(sorted((self.left, self.right)))  # type: ignore[return-value]


@dataclass(frozen=True)
class GraphSnapshot:
    """某一图版本下的完整连通性状态，全部结构只读、可哈希复算。"""

    version: int
    assertions: tuple[Assertion, ...]
    edges: tuple[tuple[str, str], ...]
    components: tuple[frozenset[str], ...]
    canonical: Mapping[str, str]

    def entity_of(self, alias: str) -> str:
        """alias 在本版本下解析到的实体键；未知 alias 为单点，返回自身。"""
        return self.canonical.get(alias, alias)


@dataclass(frozen=True)
class ApplyOutcome:
    """一条声明送入图之后的处理结果。"""

    assertion_id: str
    accepted: bool
    resend: bool
    version_before: int
    version_after: int


@dataclass(frozen=True)
class AffectedClosure:
    """一次图版本变化影响到的分量闭包。

    changed_aliases: 实体键发生变化的 alias。
    old_keys/new_keys: 受影响的旧/新实体 canonical 键全集（闭包，
    含成员构成发生变化但键名保持的实体）。
    """

    changed_aliases: tuple[str, ...]
    old_keys: tuple[str, ...]
    new_keys: tuple[str, ...]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise IdentityConflict(f"withdrawn 必须是布尔值，收到 {value!r}")


class IdentityGraph:
    """追加式版本化身份图，保存从空图（版本 0）起的全部快照。"""

    def __init__(self) -> None:
        self._states: dict[str, Assertion] = {}
        self._versions: list[GraphSnapshot] = [self._build_snapshot(0)]

    @property
    def current(self) -> GraphSnapshot:
        return self._versions[-1]

    @property
    def versions(self) -> tuple[GraphSnapshot, ...]:
        return tuple(self._versions)

    def snapshot(self, version: int) -> GraphSnapshot:
        return self._versions[version]

    def apply(self, assertion: Assertion | Mapping[str, Any]) -> ApplyOutcome:
        """接受一条声明并返回处理结果；冲突时抛 :class:`IdentityConflict`。"""
        if not isinstance(assertion, Assertion):
            assertion = Assertion.from_row(assertion)
        assert isinstance(assertion, Assertion)

        previous = self._states.get(assertion.assertion_id)
        if previous is not None:
            if assertion.revision < previous.revision:
                raise IdentityConflict(
                    f"声明 {assertion.assertion_id} 修订号 {assertion.revision} "
                    f"过期（当前为 {previous.revision}），拒绝回退"
                )
            if assertion.revision == previous.revision:
                if assertion == previous:
                    # 原样重送：幂等空操作，不产生新版本。
                    return ApplyOutcome(
                        assertion.assertion_id, True, True,
                        self.current.version, self.current.version,
                    )
                raise IdentityConflict(
                    f"声明 {assertion.assertion_id} 同一修订号 "
                    f"{assertion.revision} 内容不一致，平级内容冲突拒绝"
                )

        self._states[assertion.assertion_id] = assertion
        snapshot = self._build_snapshot(self.current.version + 1)
        self._versions.append(snapshot)
        return ApplyOutcome(
            assertion.assertion_id, True, False,
            snapshot.version - 1, snapshot.version,
        )

    def replay(self, rows: list[Mapping[str, Any]]) -> tuple[ApplyOutcome, ...]:
        return tuple(self.apply(row) for row in rows)

    def _build_snapshot(self, version: int) -> GraphSnapshot:
        """从当前有效声明边全量重算连通分量（支持撤回后的拆分）。"""
        vertices: set[str] = set()
        adjacency: dict[str, set[str]] = {}
        edges: set[tuple[str, str]] = set()

        for assertion in self._states.values():
            vertices.add(assertion.left)
            vertices.add(assertion.right)
            adjacency.setdefault(assertion.left, set())
            adjacency.setdefault(assertion.right, set())
            if assertion.withdrawn:
                continue
            edge = assertion.edge
            edges.add(edge)
            left, right = edge
            adjacency[left].add(right)
            adjacency[right].add(left)

        seen: set[str] = set()
        components: list[frozenset[str]] = []
        for start in sorted(vertices):
            if start in seen:
                continue
            queue: deque[str] = deque([start])
            seen.add(start)
            members: list[str] = []
            while queue:
                node = queue.popleft()
                members.append(node)
                for neighbor in adjacency[node]:
                    if neighbor not in seen:
                        seen.add(neighbor)
                        queue.append(neighbor)
            components.append(frozenset(members))

        components = tuple(sorted(components, key=min))
        canonical = {
            alias: min(component)
            for component in components
            for alias in component
        }
        return GraphSnapshot(
            version=version,
            assertions=tuple(
                sorted(self._states.values(), key=lambda item: item.assertion_id)
            ),
            edges=tuple(sorted(edges)),
            components=components,
            canonical=canonical,
        )


def affected_closure(old: GraphSnapshot, new: GraphSnapshot) -> AffectedClosure:
    """计算两个相邻图版本之间受影响的旧/新分量闭包。

    以实体键发生变化的 alias 为种子，分别在旧图和新图取连通分量并求并集，
    再把闭包内全部 alias 映射回旧/新实体键。这样：

    * 合并时，吸收方与被吸收方都完整纳入撤销/重记；
    * 拆分时，被拆开的整体与所有后继实体都完整纳入；
    * 成员缩小但 canonical 键名保持的实体不会被漏掉。
    """
    all_aliases = set(old.canonical) | set(new.canonical)
    changed = sorted(
        alias for alias in all_aliases
        if old.entity_of(alias) != new.entity_of(alias)
    )
    seed = set(changed)
    old_closure: set[str] = set()
    new_closure: set[str] = set()
    for component in old.components:
        if component & seed:
            old_closure |= component
    for component in new.components:
        if component & seed:
            new_closure |= component
    full_closure = old_closure | new_closure
    old_keys = sorted({old.entity_of(alias) for alias in full_closure})
    new_keys = sorted({new.entity_of(alias) for alias in full_closure})
    return AffectedClosure(tuple(changed), tuple(old_keys), tuple(new_keys))
