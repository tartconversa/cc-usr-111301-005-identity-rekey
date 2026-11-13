"""端到端管线测试：fixtures 主线、重放、冲突分类与模块劫持隔离。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.fact_ledger import FactConflict
from src.identity_graph import IdentityConflict
from src.pipeline import build_pipeline
from src.snapshot_store import SnapshotReader

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


class PipelineTests(unittest.TestCase):
    def test_fixture_headline_scenario(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = build_pipeline(
                FIXTURES / "facts.csv",
                FIXTURES / "assertions.jsonl",
                FIXTURES / "identity.json",
                tmpdir,
            )
            totals = [
                (version["version"], version["entity_totals"])
                for version in result["versions"]
            ]
            self.assertEqual(totals, [
                (0, {"a": 2, "b": 3, "c": 5}),
                (1, {"a": 5, "c": 5}),
                (2, {"a": 10}),
                (3, {"a": 5, "c": 5}),
                (4, {"a": 2, "b": 3, "c": 5}),
            ])
            # 每个图版本都金额守恒，重键不新增用量。
            self.assertTrue(
                result["conservation"]["all_versions_conserve_total"]
            )
            for event in result["rekey_events"]:
                self.assertEqual(event["new_usage_added"], 0)
                self.assertTrue(event["usage_conserved"])
            # 反例：有 a-c 路径时撤 b-c 不拆分。
            counter = result["counterexample_extra_path"]
            self.assertTrue(counter["withdraw_bridge_splits_without_path"])
            self.assertTrue(
                counter["withdraw_bridge_kept_connected_with_path"]
            )
            # 发布目录可被独立读者按 cut 读取。
            reader = SnapshotReader(tmpdir)
            self.assertEqual(reader.list_versions(), [2, 3, 4])
            self.assertEqual(
                reader.read(2).resolution["entities"]["a"]["total_usage"], 10
            )
            self.assertEqual(
                reader.read(4).resolution["grand_total_usage"], 10
            )

    def test_version_replay_reproduces_identical_snapshots(self):
        with tempfile.TemporaryDirectory() as first_dir, \
                tempfile.TemporaryDirectory() as second_dir:
            first = build_pipeline(
                FIXTURES / "facts.csv",
                FIXTURES / "assertions.jsonl",
                FIXTURES / "identity.json",
                first_dir,
            )
            second = build_pipeline(
                FIXTURES / "facts.csv",
                FIXTURES / "assertions.jsonl",
                FIXTURES / "identity.json",
                second_dir,
            )
            self.assertEqual(
                json.dumps(first["versions"], sort_keys=True),
                json.dumps(second["versions"], sort_keys=True),
            )
            # 同版本快照哈希跨进程/重放可复现。
            first_hashes = {
                version: SnapshotReader(first_dir).read(version).sha256
                for version in (2, 3, 4)
            }
            second_hashes = {
                version: SnapshotReader(second_dir).read(version).sha256
                for version in (2, 3, 4)
            }
            self.assertEqual(first_hashes, second_hashes)

    def test_label_rekey_and_fact_revision_are_reported_separately(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            facts = Path(tmpdir) / "facts.csv"
            facts.write_text(
                "fact_id,source_alias,usage\n"
                "f1,a,2\nf2,b,3\nf3,c,5\n"
                "f1,a,9\n",  # 同 fact_id 修订用量：真实事实冲突
                encoding="utf-8",
            )
            assertions = Path(tmpdir) / "assertions.jsonl"
            assertions.write_text(
                json.dumps({"assertion_id": "ab", "revision": 1,
                            "left": "a", "right": "b",
                            "withdrawn": False}) + "\n",
                encoding="utf-8",
            )
            identity = Path(tmpdir) / "identity.json"
            identity.write_text(
                json.dumps({"canonical": "minimum_alias_lexicographic",
                            "version_cuts": [1],
                            "fact_semantics": "immutable_multiset",
                            "counterexample_extra_edge": ["a", "c"]}),
                encoding="utf-8",
            )
            result = build_pipeline(facts, assertions, identity,
                                    Path(tmpdir) / "out")
            # 事实修订被单独归类拒绝，原始 f1 仍是用量 2。
            self.assertEqual(len(result["fact_conflicts"]), 1)
            self.assertEqual(
                result["fact_conflicts"][0]["kind"],
                "fact_revision_conflict",
            )
            original = next(
                fact for fact in result["ledger"]["facts"]
                if fact["fact_id"] == "f1"
            )
            self.assertEqual(original["usage"], 2)
            # 身份重键是另一类事件，明确标注零新增用量。
            self.assertTrue(all(
                event["kind"] == "entity_label_rekey"
                and event["new_usage_added"] == 0
                for event in result["rekey_events"]
            ))
            self.assertEqual(result["ledger"]["grand_total_usage"], 10)

    def test_assertion_conflicts_are_rejected_and_logged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assertions = Path(tmpdir) / "assertions.jsonl"
            assertions.write_text(
                "\n".join([
                    json.dumps({"assertion_id": "ab", "revision": 1,
                                "left": "a", "right": "b",
                                "withdrawn": False}),
                    json.dumps({"assertion_id": "ab", "revision": 1,
                                "left": "a", "right": "z",
                                "withdrawn": False}),
                    json.dumps({"assertion_id": "ab", "revision": 0,
                                "left": "a", "right": "b",
                                "withdrawn": True}),
                ]) + "\n",
                encoding="utf-8",
            )
            identity = Path(tmpdir) / "identity.json"
            identity.write_text(
                json.dumps({"version_cuts": [],
                            "counterexample_extra_edge": ["a", "c"]}),
                encoding="utf-8",
            )
            facts = Path(tmpdir) / "facts.csv"
            facts.write_text(
                "fact_id,source_alias,usage\nf1,a,2\n", encoding="utf-8"
            )
            result = build_pipeline(
                facts, assertions, identity, Path(tmpdir) / "out"
            )
            self.assertEqual(len(result["identity_conflicts"]), 2)
            # 冲突声明没有进入图。
            self.assertEqual(
                result["versions"][-1]["canonical"].get("z"), None
            )

    def test_isolated_mode_rejects_planted_module_in_data_dir(self):
        # 数据目录中植入 json.py 劫持：-I 模式 + 引导器显式路径下不受影响。
        with tempfile.TemporaryDirectory() as data_dir:
            planted = Path(data_dir) / "json.py"
            planted.write_text("raise RuntimeError('planted json.py ran')\n",
                               encoding="utf-8")
            facts = Path(data_dir) / "facts.csv"
            facts.write_text(
                "fact_id,source_alias,usage\nf1,a,2\n", encoding="utf-8"
            )
            assertions = Path(data_dir) / "assertions.jsonl"
            assertions.write_text("", encoding="utf-8")
            identity = Path(data_dir) / "identity.json"
            identity.write_text(
                json.dumps({"version_cuts": [],
                            "counterexample_extra_edge": ["a", "c"]}),
                encoding="utf-8",
            )
            process = subprocess.run(
                [sys.executable, "-I", str(ROOT / "tools/run_pipeline.py"),
                 str(facts), str(assertions), str(identity),
                 "--output-dir", str(Path(data_dir) / "out")],
                cwd=data_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertNotIn("planted json.py ran", process.stderr)

    def test_direct_exception_types_for_callers(self):
        # 库调用方可以直接捕获强类型异常而不是字符串匹配。
        from src.fact_ledger import FactLedger
        ledger = FactLedger()
        ledger.add({"fact_id": "f1", "source_alias": "a", "usage": 2})
        with self.assertRaises(FactConflict):
            ledger.add({"fact_id": "f1", "source_alias": "a", "usage": 3})
        from src.identity_graph import IdentityGraph
        graph = IdentityGraph()
        graph.apply({"assertion_id": "ab", "revision": 2,
                     "left": "a", "right": "b", "withdrawn": False})
        with self.assertRaises(IdentityConflict):
            graph.apply({"assertion_id": "ab", "revision": 1,
                         "left": "a", "right": "b", "withdrawn": True})


if __name__ == "__main__":
    unittest.main()
