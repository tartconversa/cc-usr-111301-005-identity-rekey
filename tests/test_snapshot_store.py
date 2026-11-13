"""原子发布、重启一致性与多进程读者测试。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.fact_ledger import FactLedger
from src.identity_graph import Assertion, IdentityGraph
from src.identity_resolution import ResolutionStore
from src.snapshot_store import SnapshotError, SnapshotReader, SnapshotStore

ROOT = Path(__file__).resolve().parents[1]


def build_store(tmpdir, cuts=(2, 3, 4)):
    ledger = FactLedger()
    ledger.load_rows([
        {"fact_id": "f1", "source_alias": "a", "usage": 2},
        {"fact_id": "f2", "source_alias": "b", "usage": 3},
        {"fact_id": "f3", "source_alias": "c", "usage": 5},
    ])
    graph = IdentityGraph()
    for row in [
        Assertion("ab", 1, "a", "b", False),
        Assertion("bc", 1, "b", "c", False),
        Assertion("bc", 2, "b", "c", True),
        Assertion("ab", 2, "a", "b", True),
    ]:
        graph.apply(row)
    resolution = ResolutionStore(ledger=ledger, graph=graph)
    resolution.rebuild()
    store = SnapshotStore(tmpdir)
    published = []
    for version in range(len(graph.versions)):
        is_cut = version in cuts
        if is_cut or version == graph.current.version:
            store.publish(
                graph.snapshot(version),
                resolution.snapshot(version),
                cut=is_cut,
            )
            published.append(version)
    return graph, resolution, published


class SnapshotStoreTests(unittest.TestCase):
    def test_published_snapshots_are_self_consistent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            graph, resolution, published = build_store(tmpdir)
            reader = SnapshotReader(tmpdir)
            self.assertEqual(reader.list_versions(), published)
            self.assertEqual(reader.latest_version(), graph.current.version)
            for version in published:
                snap = reader.read(version)
                totals = {
                    key: entity["total_usage"]
                    for key, entity in snap.resolution["entities"].items()
                }
                expected = {
                    key: entity.total_usage
                    for key, entity in
                    resolution.snapshot(version).entities.items()
                }
                self.assertEqual(totals, expected)
                self.assertEqual(
                    snap.resolution["grand_total_usage"], 10
                )

    def test_restart_reader_never_sees_torn_graph_and_aggregate(self):
        # 模拟重启：写入进程完全退出后，新进程读取，图身份与总额必须同版。
        with tempfile.TemporaryDirectory() as tmpdir:
            build_store(tmpdir)
            reader = SnapshotReader(tmpdir)
            latest = reader.read()
            for fact in latest.resolution["projected_facts"]:
                canonical = latest.graph["canonical"].get(
                    fact["source_alias"], fact["source_alias"]
                )
                self.assertEqual(fact["entity_key"], canonical)

    def test_tampered_snapshot_fails_hash_verification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            build_store(tmpdir)
            target = Path(tmpdir) / "snapshot-v4.json"
            document = json.loads(target.read_text(encoding="utf-8"))
            document["resolution"]["grand_total_usage"] = 999
            target.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(SnapshotError):
                SnapshotReader(tmpdir).read(4)

    def test_manifest_tamper_fails_hash_verification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            build_store(tmpdir)
            manifest_path = Path(tmpdir) / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["versions"]["4"]["sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(SnapshotError):
                SnapshotReader(tmpdir).read(4)

    def test_published_version_is_immutable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            graph, resolution, _ = build_store(tmpdir)
            store = SnapshotStore(tmpdir)
            # 重发同一快照（幂等重放）不报错。
            store.publish(
                graph.snapshot(4), resolution.snapshot(4), cut=True
            )
            # 伪造同版本号但内容不同的快照必须被拒绝（版本不可变）。
            other_graph = IdentityGraph()
            other_graph.apply(Assertion("xy", 1, "x", "y", False))
            other_ledger = FactLedger()
            other_ledger.add(
                {"fact_id": "f1", "source_alias": "x", "usage": 2}
            )
            other_resolution = ResolutionStore(
                ledger=other_ledger, graph=other_graph
            )
            other_resolution.rebuild()
            # 把版本号伪装成已发布的 4，但图/聚合内容已不同。
            fake_graph = other_graph.snapshot(1)
            fake_resolution = other_resolution.snapshot(1)
            object.__setattr__(fake_graph, "version", 4)
            object.__setattr__(fake_resolution, "version", 4)
            with self.assertRaises(SnapshotError):
                store.publish(fake_graph, fake_resolution, cut=False)

    def test_atomic_files_leave_no_temp_residue(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            build_store(tmpdir)
            leftovers = [
                name for name in os.listdir(tmpdir)
                if name.endswith(".tmp")
            ]
            self.assertEqual(leftovers, [])


class MultiProcessReaderTests(unittest.TestCase):
    def test_concurrent_reader_subprocesses_all_read_consistent_snapshots(
        self,
    ):
        # 多个独立进程并发读取已发布目录：每个进程拿到的图与聚合必须同版、
        # 哈希校验通过且金额守恒，不依赖写者进程仍在运行。
        with tempfile.TemporaryDirectory() as tmpdir:
            graph, _, published = build_store(tmpdir)
            script = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
from src.snapshot_store import SnapshotReader
reader = SnapshotReader({tmpdir!r})
for version in {published!r}:
    snap = reader.read(version)
    total = sum(
        entity["total_usage"]
        for entity in snap.resolution["entities"].values()
    )
    assert total == snap.resolution["grand_total_usage"] == 10
    assert snap.graph["version"] == snap.resolution["version"] == version
print("ok")
"""
            processes = [
                subprocess.Popen(
                    [sys.executable, "-I", "-c", script],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(4)
            ]
            for process in processes:
                stdout, stderr = process.communicate(timeout=60)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(stdout.strip(), "ok")
            self.assertEqual(SnapshotReader(tmpdir).latest_version(),
                             graph.current.version)


if __name__ == "__main__":
    unittest.main()
