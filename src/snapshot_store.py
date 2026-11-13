"""图与聚合联合的原子发布与多进程读取。

每个发布版本写一份自包含快照（图 + 投影 + 聚合 + 内容哈希），
经临时文件 + ``os.replace`` 原子落盘后才登记进 manifest。
读者要么读到上一版完整快照，要么读到新版完整快照，不可能看到
"事实已是新身份、聚合还是旧总额" 的撕裂状态；重启后同样一致。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .identity_resolution import ResolutionSnapshot
from .identity_graph import GraphSnapshot

MANIFEST_NAME = "manifest.json"
LOCK_NAME = ".publisher.lock"


class SnapshotError(RuntimeError):
    """快照存储内容损坏或不一致。"""


def _canonical_bytes(payload: Any) -> bytes:
    """确定性序列化，保证同内容哈希在进程间可复算。"""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def content_hash(payload: Any) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _graph_payload(graph: GraphSnapshot) -> dict[str, Any]:
    return {
        "version": graph.version,
        "edges": [list(edge) for edge in graph.edges],
        "components": [sorted(component) for component in graph.components],
        "canonical": dict(graph.canonical),
        "assertions": [
            {
                "assertion_id": assertion.assertion_id,
                "revision": assertion.revision,
                "left": assertion.left,
                "right": assertion.right,
                "withdrawn": assertion.withdrawn,
            }
            for assertion in graph.assertions
        ],
    }


def _resolution_payload(snapshot: ResolutionSnapshot) -> dict[str, Any]:
    return {
        "version": snapshot.version,
        "projected_facts": [
            {
                "fact_id": fact.fact_id,
                "source_alias": fact.source_alias,
                "entity_key": fact.entity_key,
                "usage": fact.usage,
            }
            for fact in snapshot.projected_facts
        ],
        "entities": {
            key: {
                "entity_key": entity.entity_key,
                "aliases": list(entity.aliases),
                "fact_ids": list(entity.fact_ids),
                "total_usage": entity.total_usage,
                "usage_by_fact": dict(entity.usage_by_fact),
            }
            for key, entity in sorted(snapshot.entities.items())
        },
        "facts_by_entity": {
            key: list(value)
            for key, value in sorted(snapshot.facts_by_entity.items())
        },
        "entity_by_fact": dict(snapshot.entity_by_fact),
        "entity_by_alias": dict(snapshot.entity_by_alias),
        "grand_total_usage": snapshot.grand_total_usage,
    }


@dataclass(frozen=True)
class PublishedSnapshot:
    version: int
    graph: dict[str, Any]
    resolution: dict[str, Any]
    sha256: str
    path: str


class SnapshotStore:
    """写者侧：加锁、原子写快照、更新 manifest。"""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.directory / LOCK_NAME
        self._manifest_path = self.directory / MANIFEST_NAME

    def publish(
        self,
        graph: GraphSnapshot,
        resolution: ResolutionSnapshot,
        *,
        cut: bool,
    ) -> PublishedSnapshot:
        if graph.version != resolution.version:
            raise SnapshotError("拒绝发布图与聚合版本号不一致的快照")

        payload = {
            "graph": _graph_payload(graph),
            "resolution": _resolution_payload(resolution),
        }
        digest = content_hash(payload)
        version = graph.version
        name = f"snapshot-v{version}.json"
        target = self.directory / name

        with _file_lock(self._lock_path):
            # 先校验 manifest（若存在），再决定是否需要重写。
            manifest = self._read_manifest_unlocked()
            existing = manifest["versions"].get(str(version))
            if existing is not None:
                if existing["sha256"] != digest:
                    raise SnapshotError(
                        f"版本 {version} 已发布且哈希不同，版本不可变"
                    )
            else:
                self._atomic_write(
                    target,
                    {
                        **payload,
                        "sha256": digest,
                        "published_cut": cut,
                    },
                )
                manifest["versions"][str(version)] = {
                    "file": name,
                    "sha256": digest,
                    "published_cut": cut,
                }
                # 新版本落盘成功后才提升 latest 指针。
                previous_latest = manifest.get("latest_version")
                manifest["latest_version"] = (
                    version if previous_latest is None
                    else max(version, previous_latest)
                )
                manifest["cuts"] = sorted(
                    set(manifest.get("cuts", [])) | ({version} if cut else set())
                )
                self._atomic_write(self._manifest_path, manifest)

        return PublishedSnapshot(
            version=version,
            graph=payload["graph"],
            resolution=payload["resolution"],
            sha256=digest,
            path=str(target),
        )

    def write_manifest_extra(self, extra: dict[str, Any]) -> None:
        """在锁内把运行清单（重键事件等）并入 manifest。"""
        with _file_lock(self._lock_path):
            manifest = self._read_manifest_unlocked()
            manifest.update(extra)
            self._atomic_write(self._manifest_path, manifest)

    def _read_manifest_unlocked(self) -> dict[str, Any]:
        if not self._manifest_path.exists():
            return {"versions": {}, "latest_version": None, "cuts": []}
        return json.loads(self._manifest_path.read_text(encoding="utf-8"))

    def _atomic_write(self, target: Path, payload: Any) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=target.name + ".", suffix=".tmp", dir=target.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True,
                          indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise


class SnapshotReader:
    """读者侧：无锁读取，校验哈希与图/聚合一致性，供多进程并发使用。"""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self._manifest_path = self.directory / MANIFEST_NAME

    def list_versions(self) -> list[int]:
        manifest = self._load_manifest()
        return sorted(int(version) for version in manifest["versions"])

    def latest_version(self) -> int:
        manifest = self._load_manifest()
        if manifest.get("latest_version") is None:
            raise SnapshotError("发布目录中还没有任何快照")
        return int(manifest["latest_version"])

    def read(self, version: int | None = None) -> PublishedSnapshot:
        manifest = self._load_manifest()
        if version is None:
            if manifest.get("latest_version") is None:
                raise SnapshotError("发布目录中还没有任何快照")
            version = int(manifest["latest_version"])
        entry = manifest["versions"].get(str(version))
        if entry is None:
            raise SnapshotError(f"版本 {version} 未发布")

        path = self.directory / entry["file"]
        document = json.loads(path.read_text(encoding="utf-8"))
        stored_hash = document.pop("sha256")
        document.pop("published_cut", None)
        actual_hash = content_hash(document)
        if stored_hash != entry["sha256"] or actual_hash != stored_hash:
            raise SnapshotError(
                f"快照版本 {version} 哈希校验失败，磁盘内容可能损坏"
            )

        graph = document["graph"]
        resolution = document["resolution"]
        self._validate_consistency(graph, resolution, version)
        return PublishedSnapshot(
            version=version,
            graph=graph,
            resolution=resolution,
            sha256=stored_hash,
            path=str(path),
        )

    def _load_manifest(self) -> dict[str, Any]:
        if not self._manifest_path.exists():
            raise SnapshotError("发布目录缺少 manifest，尚无已发布版本")
        return json.loads(self._manifest_path.read_text(encoding="utf-8"))

    def _validate_consistency(
        self,
        graph: dict[str, Any],
        resolution: dict[str, Any],
        version: int,
    ) -> None:
        """读者独立复核：连通性、投影身份、聚合金额与守恒全部对得上。"""
        if graph["version"] != version or resolution["version"] != version:
            raise SnapshotError(f"版本 {version} 快照内部版本号不一致")

        # 由边独立重算连通分量，防止图与聚合使用了被写死的旧 canonical。
        adjacency: dict[str, set[str]] = {}
        for left, right in graph["edges"]:
            adjacency.setdefault(left, set()).add(right)
            adjacency.setdefault(right, set()).add(left)
        seen: set[str] = set()
        recomputed: dict[str, str] = {}
        for start in sorted(adjacency):
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
            canonical = min(members)
            for member in members:
                recomputed[member] = canonical
        for alias, canonical in graph["canonical"].items():
            if recomputed.get(alias, alias) != canonical:
                raise SnapshotError(
                    f"版本 {version} canonical 与实际连通性不符"
                )

        # 每条投影事实的实体键必须与图一致，且原 alias 血缘保留。
        ledger_total = 0
        for fact in resolution["projected_facts"]:
            expected = graph["canonical"].get(
                fact["source_alias"], fact["source_alias"]
            )
            if fact["entity_key"] != expected:
                raise SnapshotError(
                    f"版本 {version} 事实 {fact['fact_id']} 实体键与图不一致"
                )
            if resolution["entity_by_fact"][fact["fact_id"]] != expected:
                raise SnapshotError(
                    f"版本 {version} 事实反向索引与图不一致"
                )
            ledger_total += fact["usage"]

        entity_total = sum(
            entity["total_usage"]
            for entity in resolution["entities"].values()
        )
        if entity_total != ledger_total:
            raise SnapshotError(
                f"版本 {version} 聚合总额 {entity_total} 与事实总额 "
                f"{ledger_total} 不一致"
            )
        if resolution["grand_total_usage"] != ledger_total:
            raise SnapshotError(f"版本 {version} 总额字段与事实不符")
        for key, entity in resolution["entities"].items():
            summed = sum(entity["usage_by_fact"].values())
            if summed != entity["total_usage"]:
                raise SnapshotError(
                    f"版本 {version} 实体 {key} 明细与汇总不符"
                )
            if tuple(sorted(entity["usage_by_fact"])) != tuple(
                entity["fact_ids"]
            ):
                raise SnapshotError(
                    f"版本 {version} 实体 {key} 反向索引明细不符"
                )


class _file_lock:
    """跨进程排他锁（fcntl，Linux 容器环境可用）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream = None

    def __enter__(self):
        import fcntl

        self._stream = self.path.open("a+", encoding="utf-8")
        fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        import fcntl

        assert self._stream is not None
        fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        self._stream.close()
        self._stream = None
