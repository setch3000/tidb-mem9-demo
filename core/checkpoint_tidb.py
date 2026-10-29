"""TiDB용 LangGraph 체크포인터.

langgraph-checkpoint-mysql은 TiDB가 지원하지 않는 문법(JSON_TABLE, INSERT ... AS new,
DEFAULT ('{}'))을 써서 사용할 수 없다. 이 구현은 InMemorySaver와 같은 저장 구조
(체크포인트 / 채널 값 blob / 중간 쓰기)를 TiDB 테이블 3개에 그대로 옮기고,
일반 SELECT와 INSERT ... ON DUPLICATE KEY UPDATE만 사용한다.

연결 관리(Lock, 연결 오류 시 1회 재연결·재시도)는 core.db.Database에 맡긴다.
모든 쓰기는 INSERT IGNORE / upsert라 재시도로 다시 실행돼도 안전하다.
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    SerializerProtocol,
    get_checkpoint_id,
    get_checkpoint_metadata,
    writes_sort_key,
)

from core.db import Database

SETUP_SQL = [
    """
    CREATE TABLE IF NOT EXISTS lg_checkpoints (
      thread_id            VARCHAR(150) NOT NULL,
      checkpoint_ns        VARCHAR(255) NOT NULL DEFAULT '',
      checkpoint_id        VARCHAR(64)  NOT NULL,
      parent_checkpoint_id VARCHAR(64),
      step                 INT,
      source               VARCHAR(32),
      checkpoint_type      VARCHAR(32)  NOT NULL,
      checkpoint           LONGBLOB     NOT NULL,
      metadata_type        VARCHAR(32)  NOT NULL,
      metadata             LONGBLOB     NOT NULL,
      created_at           TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lg_checkpoint_blobs (
      thread_id     VARCHAR(150) NOT NULL,
      checkpoint_ns VARCHAR(255) NOT NULL DEFAULT '',
      channel       VARCHAR(150) NOT NULL,
      version       VARCHAR(64)  NOT NULL,
      type          VARCHAR(32)  NOT NULL,
      `blob`        LONGBLOB,
      PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lg_checkpoint_writes (
      thread_id     VARCHAR(150) NOT NULL,
      checkpoint_ns VARCHAR(255) NOT NULL DEFAULT '',
      checkpoint_id VARCHAR(64)  NOT NULL,
      task_id       VARCHAR(64)  NOT NULL,
      idx           INT          NOT NULL,
      task_path     VARCHAR(512) NOT NULL DEFAULT '',
      channel       VARCHAR(150) NOT NULL,
      type          VARCHAR(32)  NOT NULL,
      `blob`        LONGBLOB     NOT NULL,
      PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
    )
    """,
]

INSERT_WRITE_SQL = """
    INSERT {ignore} INTO lg_checkpoint_writes
      (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, task_path, channel, type, `blob`)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    {upsert}
"""
UPSERT_WRITE_CLAUSE = (
    "ON DUPLICATE KEY UPDATE channel = VALUES(channel), type = VALUES(type), `blob` = VALUES(`blob`)"
)


class TiDBSaver(BaseCheckpointSaver[str]):
    def __init__(self, db: Database, *, serde: SerializerProtocol | None = None) -> None:
        super().__init__(serde=serde)
        self._run = db.run

    def setup(self) -> None:
        def run(cur) -> None:
            for sql in SETUP_SQL:
                cur.execute(sql)

        self._run(run)

    # ---- 읽기 ----

    def _load_blobs(self, cur, thread_id: str, ns: str, versions: ChannelVersions) -> dict[str, Any]:
        if not versions:
            return {}
        pairs = list(versions.items())
        cur.execute(
            f"""
            SELECT channel, type, `blob` FROM lg_checkpoint_blobs
            WHERE thread_id = %s AND checkpoint_ns = %s
              AND (channel, version) IN ({", ".join(["(%s, %s)"] * len(pairs))})
            """,
            [thread_id, ns, *(str(x) for pair in pairs for x in pair)],
        )
        return {
            channel: self.serde.loads_typed((type_, blob))
            for channel, type_, blob in cur.fetchall()
            if type_ != "empty"
        }

    def _load_writes(self, cur, thread_id: str, ns: str, checkpoint_id: str) -> list:
        cur.execute(
            """
            SELECT task_id, idx, task_path, channel, type, `blob` FROM lg_checkpoint_writes
            WHERE thread_id = %s AND checkpoint_ns = %s AND checkpoint_id = %s
            """,
            (thread_id, ns, checkpoint_id),
        )
        rows = sorted(cur.fetchall(), key=lambda r: writes_sort_key(r[2], r[0], r[1]))
        return [(task_id, channel, self.serde.loads_typed((type_, blob)))
                for task_id, _, _, channel, type_, blob in rows]

    def _to_tuple(self, cur, row) -> CheckpointTuple:
        thread_id, ns, checkpoint_id, parent_id, cp_type, cp_blob, md_type, md_blob = row
        checkpoint: Checkpoint = self.serde.loads_typed((cp_type, cp_blob))
        return CheckpointTuple(
            config={"configurable": {
                "thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": checkpoint_id}},
            checkpoint={
                **checkpoint,
                "channel_values": self._load_blobs(cur, thread_id, ns, checkpoint["channel_versions"]),
            },
            metadata=self.serde.loads_typed((md_type, md_blob)),
            parent_config=(
                {"configurable": {
                    "thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": parent_id}}
                if parent_id else None
            ),
            pending_writes=self._load_writes(cur, thread_id, ns, checkpoint_id),
        )

    _SELECT = """
        SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
               checkpoint_type, checkpoint, metadata_type, metadata
        FROM lg_checkpoints
    """

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)

        def run(cur) -> CheckpointTuple | None:
            if checkpoint_id:
                cur.execute(
                    self._SELECT + " WHERE thread_id = %s AND checkpoint_ns = %s AND checkpoint_id = %s",
                    (thread_id, ns, checkpoint_id),
                )
            else:
                cur.execute(
                    self._SELECT + " WHERE thread_id = %s AND checkpoint_ns = %s"
                    " ORDER BY checkpoint_id DESC LIMIT 1",
                    (thread_id, ns),
                )
            row = cur.fetchone()
            return self._to_tuple(cur, row) if row else None

        return self._run(run)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        where, params = [], []
        if config:
            where.append("thread_id = %s")
            params.append(config["configurable"]["thread_id"])
            if (ns := config["configurable"].get("checkpoint_ns")) is not None:
                where.append("checkpoint_ns = %s")
                params.append(ns)
            if checkpoint_id := get_checkpoint_id(config):
                where.append("checkpoint_id = %s")
                params.append(checkpoint_id)
        if before and (before_id := get_checkpoint_id(before)):
            where.append("checkpoint_id < %s")
            params.append(before_id)
        sql = self._SELECT + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY checkpoint_id DESC"
        if limit is not None and not filter:
            sql += f" LIMIT {int(limit)}"

        def run(cur) -> list[CheckpointTuple]:
            cur.execute(sql, params)
            results = []
            for row in cur.fetchall():
                if limit is not None and len(results) >= limit:
                    break
                tup = self._to_tuple(cur, row)
                if filter and not all(tup.metadata.get(k) == v for k, v in filter.items()):
                    continue
                results.append(tup)
            return results

        yield from self._run(run)

    # ---- 쓰기 ----

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        c = checkpoint.copy()
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        values: dict[str, Any] = c.pop("channel_values")  # type: ignore[misc]
        blobs = []
        for channel, version in new_versions.items():
            type_, blob = (self.serde.dumps_typed(values[channel])
                           if channel in values else ("empty", b""))
            blobs.append((thread_id, ns, channel, str(version), type_, blob))
        full_metadata = get_checkpoint_metadata(config, metadata)
        cp_type, cp_blob = self.serde.dumps_typed(c)
        md_type, md_blob = self.serde.dumps_typed(full_metadata)

        # 트랜잭션 없이 blob → 체크포인트 순서로 쓴다. 중간에 실패해도 남는 것은
        # 참조되지 않는 blob뿐이고, blob이 빠진 체크포인트는 생기지 않는다.
        def run(cur) -> None:
            if blobs:
                cur.executemany(
                    "INSERT IGNORE INTO lg_checkpoint_blobs"
                    " (thread_id, checkpoint_ns, channel, version, type, `blob`)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    blobs,
                )
            cur.execute(
                """
                INSERT INTO lg_checkpoints
                  (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, step, source,
                   checkpoint_type, checkpoint, metadata_type, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  checkpoint_type = VALUES(checkpoint_type), checkpoint = VALUES(checkpoint),
                  metadata_type = VALUES(metadata_type), metadata = VALUES(metadata)
                """,
                (thread_id, ns, checkpoint["id"], config["configurable"].get("checkpoint_id"),
                 full_metadata.get("step"), full_metadata.get("source"),
                 cp_type, cp_blob, md_type, md_blob),
            )

        self._run(run)
        return {"configurable": {
            "thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": checkpoint["id"]}}

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]
        # 일반 쓰기는 이미 있으면 유지하고(INSERT IGNORE), 특수 채널(음수 idx)은 덮어쓴다
        keep, overwrite = [], []
        for idx, (channel, value) in enumerate(writes):
            idx = WRITES_IDX_MAP.get(channel, idx)
            type_, blob = self.serde.dumps_typed(value)
            row = (thread_id, ns, checkpoint_id, task_id, idx, task_path, channel, type_, blob)
            (overwrite if idx < 0 else keep).append(row)
        def run(cur) -> None:
            if keep:
                cur.executemany(INSERT_WRITE_SQL.format(ignore="IGNORE", upsert=""), keep)
            if overwrite:
                cur.executemany(
                    INSERT_WRITE_SQL.format(ignore="", upsert=UPSERT_WRITE_CLAUSE), overwrite)

        self._run(run, transaction=bool(keep and overwrite))

    def delete_thread(self, thread_id: str) -> None:
        def run(cur) -> None:
            for table in ("lg_checkpoints", "lg_checkpoint_blobs", "lg_checkpoint_writes"):
                cur.execute(f"DELETE FROM {table} WHERE thread_id = %s", (thread_id,))

        self._run(run, transaction=True)

    def get_next_version(self, current: str | None, channel: None) -> str:
        # InMemorySaver와 같은 형식: 정렬 가능한 정수부 + 충돌 방지용 난수
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            current_v = int(current.split(".")[0])
        return f"{current_v + 1:032}.{random.random():016}"

    # ---- 비동기: 동기 구현을 스레드에서 실행 ----

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        items = await asyncio.to_thread(
            lambda: list(self.list(config, filter=filter, before=before, limit=limit)))
        for item in items:
            yield item

    async def aput(self, config, checkpoint, metadata, new_versions) -> RunnableConfig:
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path: str = "") -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)
