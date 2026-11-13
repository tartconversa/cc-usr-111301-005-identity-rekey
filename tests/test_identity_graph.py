"""版本化身份图测试：环、桥、自环、单点、重送与平级冲突。"""
import unittest

from src.identity_graph import (
    Assertion,
    IdentityConflict,
    IdentityGraph,
    affected_closure,
)


def edge(assertion_id, left, right, *, revision=1, withdrawn=False):
    return Assertion(
        assertion_id=assertion_id,
        revision=revision,
        left=left,
        right=right,
        withdrawn=withdrawn,
    )


class IdentityGraphTests(unittest.TestCase):
    def test_unknown_alias_is_singleton_entity(self):
        graph = IdentityGraph()
        # 没有任何声明的 alias 是单点，实体键即自身。
        self.assertEqual(graph.current.entity_of("lonely"), "lonely")
        self.assertEqual(graph.current.components, ())

    def test_self_loop_does_not_change_components(self):
        graph = IdentityGraph()
        graph.apply(edge("loop", "x", "x"))
        snapshot = graph.current
        self.assertEqual(snapshot.edges, (("x", "x"),))
        self.assertEqual(snapshot.components, (frozenset({"x"}),))
        self.assertEqual(snapshot.entity_of("x"), "x")
        # 自环撤回后仍是单点。
        graph.apply(edge("loop", "x", "x", revision=2, withdrawn=True))
        self.assertEqual(graph.current.components, (frozenset({"x"}),))

    def test_undirected_edge_and_lexicographic_canonical(self):
        graph = IdentityGraph()
        graph.apply(edge("e1", "zeta", "alpha"))
        snapshot = graph.current
        self.assertEqual(snapshot.entity_of("zeta"), "alpha")
        self.assertEqual(snapshot.entity_of("alpha"), "alpha")
        self.assertEqual(
            snapshot.components, (frozenset({"alpha", "zeta"}),)
        )

    def test_bridge_withdrawal_splits_component(self):
        # a-b、b-c 合并为整体，撤 b-c 后按实际连通性拆分。
        graph = IdentityGraph()
        graph.apply(edge("ab", "a", "b"))
        graph.apply(edge("bc", "b", "c"))
        self.assertEqual(
            graph.current.components,
            (frozenset({"a", "b", "c"}),),
        )
        graph.apply(edge("bc", "b", "c", revision=2, withdrawn=True))
        self.assertEqual(
            sorted(graph.current.components, key=min),
            [frozenset({"a", "b"}), frozenset({"c"})],
        )
        # 再撤 a-b 恢复三个单点。
        graph.apply(edge("ab", "a", "b", revision=2, withdrawn=True))
        self.assertEqual(
            sorted(graph.current.components, key=min),
            [frozenset({"a"}), frozenset({"b"}), frozenset({"c"})],
        )

    def test_cycle_withdrawal_does_not_split(self):
        # 环 a-b、b-c、a-c：撤 b-c 后仍有 a-c 路径，不拆分。
        graph = IdentityGraph()
        graph.apply(edge("ab", "a", "b"))
        graph.apply(edge("bc", "b", "c"))
        graph.apply(edge("ac", "a", "c"))
        self.assertEqual(len(graph.current.components), 1)
        graph.apply(edge("bc", "b", "c", revision=2, withdrawn=True))
        self.assertEqual(
            graph.current.components,
            (frozenset({"a", "b", "c"}),),
            "环上撤边必须按实际连通性保持单分量",
        )

    def test_canonical_recomputed_after_split_not_pinned(self):
        # 不能用旧 canonical 永久写死：拆分后新实体取各自最小 alias。
        graph = IdentityGraph()
        graph.apply(edge("ab", "a", "b"))
        graph.apply(edge("bc", "b", "c"))
        self.assertEqual(graph.current.canonical["c"], "a")
        graph.apply(edge("bc", "b", "c", revision=2, withdrawn=True))
        self.assertEqual(graph.current.canonical["c"], "c")
        self.assertEqual(graph.current.canonical["b"], "a")

    def test_resend_is_idempotent_no_new_version(self):
        graph = IdentityGraph()
        first = graph.apply(edge("ab", "a", "b"))
        resent = graph.apply(edge("ab", "a", "b"))
        self.assertFalse(first.resend)
        self.assertTrue(resent.resend)
        self.assertEqual(resent.version_before, resent.version_after)
        self.assertEqual(len(graph.versions), 2)  # 仅版本 0、1
        # 撤回声明的重送同样幂等。
        withdrawn = edge("ab", "a", "b", revision=2, withdrawn=True)
        graph.apply(withdrawn)
        graph.apply(withdrawn)
        self.assertEqual(graph.current.version, 2)

    def test_peer_content_conflict_same_revision_rejected(self):
        graph = IdentityGraph()
        graph.apply(edge("e1", "a", "b"))
        with self.assertRaises(IdentityConflict):
            # 同 assertion_id、同 revision 但端点被更正，属于平级冲突。
            graph.apply(edge("e1", "a", "d"))
        # 图状态保持冲突前不变。
        self.assertEqual(graph.current.entity_of("b"), "a")
        self.assertEqual(graph.current.entity_of("d"), "d")

    def test_stale_revision_rejected(self):
        graph = IdentityGraph()
        graph.apply(edge("e1", "a", "b", revision=3))
        with self.assertRaises(IdentityConflict):
            graph.apply(edge("e1", "a", "b", revision=2, withdrawn=True))
        with self.assertRaises(IdentityConflict):
            graph.apply(edge("e1", "a", "b", revision=1))
        # 合法的新修订（更正端点）可以接受。
        graph.apply(edge("e1", "a", "c", revision=4))
        self.assertEqual(graph.current.entity_of("c"), "a")
        self.assertEqual(graph.current.entity_of("b"), "b")

    def test_full_version_replay_is_deterministic(self):
        rows = [
            {"assertion_id": "ab", "revision": 1, "left": "a",
             "right": "b", "withdrawn": False},
            {"assertion_id": "bc", "revision": 1, "left": "b",
             "right": "c", "withdrawn": False},
            {"assertion_id": "bc", "revision": 2, "left": "b",
             "right": "c", "withdrawn": True},
            {"assertion_id": "ab", "revision": 2, "left": "a",
             "right": "b", "withdrawn": True},
        ]
        first = IdentityGraph()
        first.replay(rows)
        second = IdentityGraph()
        second.replay(rows)
        self.assertEqual(
            [snapshot.components for snapshot in first.versions],
            [snapshot.components for snapshot in second.versions],
        )
        self.assertEqual(
            [dict(snapshot.canonical) for snapshot in first.versions],
            [dict(snapshot.canonical) for snapshot in second.versions],
        )

    def test_affected_closure_merge_and_split(self):
        graph = IdentityGraph()
        graph.apply(edge("ab", "a", "b"))
        graph.apply(edge("bc", "b", "c"))
        merge_closure = affected_closure(graph.snapshot(1), graph.snapshot(2))
        self.assertEqual(merge_closure.changed_aliases, ("c",))
        self.assertEqual(merge_closure.old_keys, ("a", "c"))
        self.assertEqual(merge_closure.new_keys, ("a",))

        graph.apply(edge("bc", "b", "c", revision=2, withdrawn=True))
        split_closure = affected_closure(graph.snapshot(2), graph.snapshot(3))
        self.assertEqual(split_closure.changed_aliases, ("c",))
        # 拆分时旧整体与两个新实体都在闭包内。
        self.assertEqual(split_closure.old_keys, ("a",))
        self.assertEqual(split_closure.new_keys, ("a", "c"))


if __name__ == "__main__":
    unittest.main()
