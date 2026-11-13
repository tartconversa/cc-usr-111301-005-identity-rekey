"""端到端身份解析与事实重键管线。

把只负责加载的 :mod:`src.identity_reader` 接到版本化身份图、不可变事实
账本、解析投影和原子快照发布上。运行结果同时给出：

* 每个图版本的连通分量、canonical 键与实体汇总；
* 每次声明变化的受影响分量闭包与重键事件（金额守恒）；
* 明确区分"实体标签变化（重键，零新增用量）"与"真实事实修订
  （FactConflict，拒绝而不是改写）"；
* 反例场景：额外存在 a-c 路径时撤回 b-c 不拆分。
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .fact_ledger import FactConflict, FactLedger
from .identity_graph import (
    Assertion,
    GraphSnapshot,
    IdentityConflict,
    IdentityGraph,
)
from .identity_reader import load
from .identity_resolution import ResolutionStore
from .snapshot_store import SnapshotStore, content_hash


def _rekey_payload(event) -> dict[str, Any]:
    return {
        "version_from": event.version_from,
        "version_to": event.version_to,
        "changed_aliases": list(event.changed_aliases),
        "affected_old_entities": list(event.old_keys),
        "affected_new_entities": list(event.new_keys),
        "withdrawn_entity_totals": {
            entity.entity_key: entity.total_usage
            for entity in event.withdrawn_entities
        },
        "added_entity_totals": {
            entity.entity_key: entity.total_usage
            for entity in event.added_entities
        },
        "moved_usage": event.moved_usage,
        "usage_conserved": event.usage_conserved,
        "kind": "entity_label_rekey",
        "new_usage_added": 0,
    }


def _version_payload(graph: GraphSnapshot, resolution) -> dict[str, Any]:
    return {
        "version": graph.version,
        "edges": [list(edge) for edge in graph.edges],
        "components": [sorted(component) for component in graph.components],
        "canonical": dict(graph.canonical),
        "entity_totals": {
            key: entity.total_usage
            for key, entity in resolution.entities.items()
        },
        "entity_facts": {
            key: list(entity.fact_ids)
            for key, entity in resolution.entities.items()
        },
        "grand_total_usage": resolution.grand_total_usage,
    }


def build_pipeline(
    facts_path: str | Path,
    assertions_path: str | Path,
    identity_path: str | Path,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """运行完整管线；output_dir 给定时把图与聚合原子发布到该目录。"""
    config = load(identity_path)
    cuts = set(config.get("version_cuts", []))

    # --- 原始事实：只加载，不覆盖 source_alias，事实冲突显式分类 ---
    ledger = FactLedger()
    fact_conflicts: list[dict[str, str]] = []
    fact_rows = load(facts_path)
    for row in fact_rows:
        try:
            ledger.add(row)
        except FactConflict as exc:
            # 真实事实修订/平级冲突：拒绝并单独报告，绝不静默改写，
            # 也不与实体标签重键混为一谈。
            fact_conflicts.append({
                "kind": "fact_revision_conflict",
                "fact_id": str(row.get("fact_id", "")),
                "reason": str(exc),
            })

    # --- 身份声明：版本化等同图 ---
    graph = IdentityGraph()
    assertion_rows = load(assertions_path)
    apply_log: list[dict[str, Any]] = []
    identity_conflicts: list[dict[str, str]] = []
    for row in assertion_rows:
        try:
            outcome = graph.apply(row)
        except IdentityConflict as exc:
            identity_conflicts.append({
                "kind": "assertion_conflict",
                "assertion_id": str(row.get("assertion_id", "")),
                "reason": str(exc),
            })
            continue
        apply_log.append({
            "assertion_id": outcome.assertion_id,
            "resend": outcome.resend,
            "version_before": outcome.version_before,
            "version_after": outcome.version_after,
        })

    # --- 每个图版本成对生成投影与聚合 ---
    resolution = ResolutionStore(ledger=ledger, graph=graph)
    resolution.rebuild()
    events = resolution.rekey_events()

    # 守恒总检查：任意版本下实体汇总之和恒等于原始事实总量。
    ledger_total = ledger.total_usage()
    for snapshot in resolution.snapshots:
        if snapshot.grand_total_usage != ledger_total:
            raise RuntimeError(
                f"版本 {snapshot.version} 金额不守恒: "
                f"{snapshot.grand_total_usage} != {ledger_total}"
            )

    # --- 反例：额外 a-c 路径存在时，撤回 b-c 不拆分 ---
    counterexample = _counterexample(config, assertion_rows)

    result: dict[str, Any] = {
        "config_hash": content_hash(config),
        "ledger": {
            "fact_count": len(ledger.facts),
            "fact_resends": ledger.resends,
            "grand_total_usage": ledger_total,
            "facts": [asdict(fact) for fact in ledger.facts],
        },
        "assertion_log": apply_log,
        "identity_conflicts": identity_conflicts,
        "fact_conflicts": fact_conflicts,
        "versions": [
            _version_payload(graph.snapshot(snapshot.version), snapshot)
            for snapshot in resolution.snapshots
        ],
        "rekey_events": [_rekey_payload(event) for event in events],
        "counterexample_extra_path": counterexample,
        "conservation": {
            "ledger_total_usage": ledger_total,
            "all_versions_conserve_total": all(
                event.usage_conserved for event in events
            ),
        },
    }

    if output_dir is not None:
        result["publication"] = _publish(
            output_dir, graph, resolution, cuts, config, result
        )
    return result


def _counterexample(
    config: dict[str, Any], assertion_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """按 identity.json 的 counterexample_extra_edge 构造对照图。

    两条图同步推进同一条声明序列；对照图在任何撤回发生前就额外持有
    ``a-c`` 边，因此撤回 ``b-c`` 时可直接比较"桥边 vs 环路边"的差异。
    """
    extra = tuple(config.get("counterexample_extra_edge", ()))
    if len(extra) != 2:
        return {"configured": False}
    extra_left, extra_right = extra

    base_graph = IdentityGraph()
    alt_graph = IdentityGraph()
    alt_conflicts: list[str] = []
    alt_graph.apply(Assertion(
        assertion_id="counterexample-extra",
        revision=1,
        left=extra_left,
        right=extra_right,
        withdrawn=False,
    ))
    # 记录每条有效（非重送）声明后两张图各自的版本号，用于对齐
    # "同一条撤回声明"在两张图中的落点。
    base_after: list[int] = []
    alt_after: list[int] = []
    withdrawn_ordinal: int | None = None
    accepted_ordinal = 0
    for row in assertion_rows:
        row_withdrawn = str(row.get("withdrawn", "")).lower() == "true"
        try:
            base_outcome = base_graph.apply(row)
            alt_graph.apply(row)
        except IdentityConflict as exc:
            alt_conflicts.append(str(exc))
            continue
        if base_outcome.resend:
            continue
        accepted_ordinal += 1
        # 锚定首次撤回（题目中的首个桥边拆分点 b-c）。
        if row_withdrawn and withdrawn_ordinal is None:
            withdrawn_ordinal = accepted_ordinal
        base_after.append(base_graph.current.version)
        alt_after.append(alt_graph.current.version)

    if withdrawn_ordinal is None:
        return {
            "configured": True,
            "extra_edge": [extra_left, extra_right],
            "note": "声明序列中没有撤回事件，无法构造桥边对照",
            "alt_conflicts": alt_conflicts,
        }
    # 对齐到该撤回声明被接受后两张图的版本。
    base_version = base_after[withdrawn_ordinal - 1]
    alt_version = alt_after[withdrawn_ordinal - 1]
    withdrawn_row = next(
        row for row in assertion_rows
        if str(row.get("withdrawn", "")).lower() == "true"
    )
    return {
        "configured": True,
        "extra_edge": [extra_left, extra_right],
        "withdrawn_edge": sorted(
            (str(withdrawn_row["left"]), str(withdrawn_row["right"]))
        ),
        "components_without_extra_edge": [
            sorted(component)
            for component in base_graph.snapshot(base_version).components
        ],
        "components_with_extra_edge": [
            sorted(component)
            for component in alt_graph.snapshot(alt_version).components
        ],
        "withdraw_bridge_splits_without_path": (
            len(base_graph.snapshot(base_version).components)
            > len(base_graph.snapshot(base_version - 1).components)
        ),
        "withdraw_bridge_kept_connected_with_path": (
            len(alt_graph.snapshot(alt_version).components) == 1
        ),
        "alt_conflicts": alt_conflicts,
    }


def _publish(
    output_dir: str | Path,
    graph: IdentityGraph,
    resolution: ResolutionStore,
    cuts: set[int],
    config: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    store = SnapshotStore(output_dir)
    published: list[int] = []
    for version in range(len(graph.versions)):
        is_cut = version in cuts
        # 配置的 cut 与最新版本必须发布；其余版本按需发布。
        if is_cut or version == graph.current.version:
            store.publish(
                graph.snapshot(version),
                resolution.snapshot(version),
                cut=is_cut,
            )
            published.append(version)
    store.write_manifest_extra({
        "cuts_configured": sorted(cuts),
        "rekey_events": result["rekey_events"],
        "counterexample_extra_path": result["counterexample_extra_path"],
        "conservation": result["conservation"],
        "config_hash": result["config_hash"],
    })
    return {
        "directory": str(Path(output_dir)),
        "published_versions": published,
        "latest_version": graph.current.version,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="版本化身份解析与可逆事实重键管线（标准库）"
    )
    parser.add_argument("facts_path", help="原始事实 CSV，例如 fixtures/facts.csv")
    parser.add_argument("assertions_path", help="等同声明 JSONL")
    parser.add_argument("identity_path", help="解析配置 identity.json")
    parser.add_argument(
        "--output-dir",
        default=".workspace-output",
        help="原子快照发布目录（默认 .workspace-output）",
    )
    args = parser.parse_args()
    result = build_pipeline(
        args.facts_path,
        args.assertions_path,
        args.identity_path,
        args.output_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
