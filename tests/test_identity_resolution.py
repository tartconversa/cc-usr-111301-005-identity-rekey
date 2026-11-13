"""事实解析投影、聚合反向索引与重键守恒测试。"""
import unittest

from src.fact_ledger import FactLedger
from src.identity_graph import Assertion, IdentityGraph
from src.identity_resolution import ResolutionStore


def assertion(assertion_id, left, right, *, revision=1, withdrawn=False):
    return Assertion(
        assertion_id=assertion_id, revision=revision,
        left=left, right=right, withdrawn=withdrawn,
    )


def build(rows=()):
    ledger = FactLedger()
    ledger.load_rows([
        {"fact_id": "f1", "source_alias": "a", "usage": 2},
        {"fact_id": "f2", "source_alias": "b", "usage": 3},
        {"fact_id": "f3", "source_alias": "c", "usage": 5},
    ])
    graph = IdentityGraph()
    for row in rows:
        graph.apply(row)
    store = ResolutionStore(ledger=ledger, graph=graph)
    store.rebuild()
    return ledger, graph, store


class ResolutionTests(unittest.TestCase):
    def test_headline_merge_split_restore_totals(self):
        _, graph, store = build([
            assertion("ab", "a", "b"),
            assertion("bc", "b", "c"),
            assertion("bc", "b", "c", revision=2, withdrawn=True),
            assertion("ab", "a", "b", revision=2, withdrawn=True),
        ])
        totals = [
            {key: entity.total_usage
             for key, entity in snapshot.entities.items()}
            for snapshot in store.snapshots
        ]
        self.assertEqual(totals[0], {"a": 2, "b": 3, "c": 5})
        self.assertEqual(totals[1], {"a": 5, "c": 5})
        self.assertEqual(totals[2], {"a": 10})
        self.assertEqual(totals[3], {"a": 5, "c": 5})
        self.assertEqual(totals[4], {"a": 2, "b": 3, "c": 5})
        for snapshot in store.snapshots:
            self.assertEqual(snapshot.grand_total_usage, 10)

    def test_projected_facts_never_lose_source_lineage(self):
        _, _, store = build([assertion("ab", "a", "b"),
                             assertion("bc", "b", "c")])
        current = store.current
        by_fact = {fact.fact_id: fact for fact in current.projected_facts}
        # 合并到实体 a，但每条事实的原始 source_alias 血缘保留。
        self.assertEqual(by_fact["f1"].source_alias, "a")
        self.assertEqual(by_fact["f2"].source_alias, "b")
        self.assertEqual(by_fact["f3"].source_alias, "c")
        self.assertEqual({fact.entity_key for fact in current.projected_facts},
                         {"a"})
        self.assertEqual(len(current.projected_facts), 3)

    def test_reverse_indexes_are_coherent(self):
        _, _, store = build([assertion("ab", "a", "b")])
        snapshot = store.current
        self.assertEqual(snapshot.entity_by_fact["f2"], "a")
        self.assertEqual(snapshot.entity_by_alias["b"], "a")
        self.assertEqual(snapshot.facts_by_entity["a"], ("f1", "f2"))
        self.assertEqual(snapshot.facts_by_entity["c"], ("f3",))
        # 反查每个实体的明细加总等于实体汇总。
        for key, fact_ids in snapshot.facts_by_entity.items():
            entity = snapshot.entities[key]
            self.assertEqual(
                sum(snapshot.entities[key].usage_by_fact[fid]
                    for fid in fact_ids),
                entity.total_usage,
            )

    def test_rekey_events_conserve_money_and_report_affected(self):
        _, _, store = build([
            assertion("ab", "a", "b"),
            assertion("bc", "b", "c"),
            assertion("bc", "b", "c", revision=2, withdrawn=True),
            assertion("ab", "a", "b", revision=2, withdrawn=True),
        ])
        events = store.rekey_events()
        self.assertEqual(len(events), 4)
        expected_moved = [5, 10, 10, 5]
        for event, moved in zip(events, expected_moved):
            self.assertTrue(event.usage_conserved)
            self.assertEqual(event.moved_usage, moved)
        # 拆分事件：旧实体撤销 10，两个新实体分别记入 5。
        split = events[2]
        self.assertEqual(split.old_keys, ("a",))
        self.assertEqual(split.new_keys, ("a", "c"))
        self.assertEqual(
            {e.entity_key: e.total_usage for e in split.withdrawn_entities},
            {"a": 10},
        )
        self.assertEqual(
            {e.entity_key: e.total_usage for e in split.added_entities},
            {"a": 5, "c": 5},
        )

    def test_rekey_is_label_change_not_new_usage(self):
        ledger, _, store = build([
            assertion("ab", "a", "b"),
            assertion("bc", "b", "c"),
            assertion("bc", "b", "c", revision=2, withdrawn=True),
        ])
        before = store.snapshot(0).grand_total_usage
        after = store.current.grand_total_usage
        self.assertEqual(before, after)
        self.assertEqual(after, ledger.total_usage())
        # 每条事实的用量在所有版本里逐字不变。
        for snapshot in store.snapshots:
            usages = sorted(fact.usage for fact in snapshot.projected_facts)
            self.assertEqual(usages, [2, 3, 5])

    def test_alias_never_used_still_resolves_with_zero_total(self):
        graph = IdentityGraph()
        graph.apply(assertion("az", "a", "z"))  # z 没有任何事实
        ledger = FactLedger()
        ledger.load_rows([
            {"fact_id": "f1", "source_alias": "a", "usage": 2},
        ])
        store = ResolutionStore(ledger=ledger, graph=graph)
        store.rebuild()
        snapshot = store.current
        self.assertEqual(snapshot.entity_by_alias["z"], "a")
        self.assertEqual(snapshot.entities["a"].aliases, ("a", "z"))
        self.assertEqual(snapshot.entities["a"].fact_ids, ("f1",))
        self.assertEqual(snapshot.entities["a"].total_usage, 2)
        self.assertEqual(snapshot.grand_total_usage, 2)

    def test_cycle_withdrawal_keeps_single_aggregate(self):
        _, _, store = build([
            assertion("ab", "a", "b"),
            assertion("bc", "b", "c"),
            assertion("ac", "a", "c"),
            assertion("bc", "b", "c", revision=2, withdrawn=True),
        ])
        current = store.current
        self.assertEqual(list(current.entities), ["a"])
        self.assertEqual(current.entities["a"].total_usage, 10)
        events = store.rekey_events()
        # 撤回环路边不产生重键（连通性未变）。
        self.assertEqual(events[-1].changed_aliases, ())
        self.assertEqual(events[-1].moved_usage, 0)

    def test_equal_amount_facts_sum_under_merged_entity(self):
        ledger = FactLedger()
        ledger.load_rows([
            {"fact_id": "g1", "source_alias": "a", "usage": 4},
            {"fact_id": "g2", "source_alias": "b", "usage": 4},
        ])
        graph = IdentityGraph()
        graph.apply(assertion("ab", "a", "b"))
        store = ResolutionStore(ledger=ledger, graph=graph)
        store.rebuild()
        entity = store.current.entities["a"]
        self.assertEqual(entity.fact_ids, ("g1", "g2"))
        self.assertEqual(entity.total_usage, 8)  # 绝不能去重成 4

    def test_historical_versions_remain_queryable(self):
        _, graph, store = build([
            assertion("ab", "a", "b"),
            assertion("bc", "b", "c"),
            assertion("bc", "b", "c", revision=2, withdrawn=True),
        ])
        self.assertEqual(store.snapshot(2).entities["a"].total_usage, 10)
        self.assertEqual(store.snapshot(3).entities["a"].total_usage, 5)
        self.assertEqual(store.snapshot(3).entities["c"].total_usage, 5)
        # 图版本与投影版本一一对应。
        self.assertEqual(len(store.snapshots), len(graph.versions))


if __name__ == "__main__":
    unittest.main()
