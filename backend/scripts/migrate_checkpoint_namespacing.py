#!/usr/bin/env python3
"""
One-off migration for audit item T09 (checkpoint tenant-namespacing).

Background
----------
`MongoDBSaver` keys LangGraph checkpoints on `thread_id`, and `thread_id` was
entirely client-supplied — a path parameter on `/ws/chat/{thread_id}`, a body
field on `/api/query`, `console_thread_id` on the embed session. Two tenants
using the same id shared one conversation state, so tenant A could resume tenant
B's message history, extracted lead PII and prior tool results.

Checkpoints are now stored under `"<tenant_id>::<thread_id>"`. Rows written
before that change are keyed on the bare id and are invisible to the new code —
in-flight dashboard conversations would silently lose their agent memory
(transcripts in `conversations` are unaffected).

This script re-keys them.

Resolving the owner
-------------------
The checkpoint documents carry no tenant field, so ownership is recovered from
    conversations   (tenant_id, thread_id)
    voice_call_links / voice_call_sessions   for vapi_* and embed_* threads

A thread id claimed by more than one tenant is exactly the collision the fix
prevents. Those are never guessed at: they are reported and left alone, and
`--purge-ambiguous` can delete them so the affected conversations simply start
fresh.

Usage
-----
    python -m backend.scripts.migrate_checkpoint_namespacing            # dry run
    python -m backend.scripts.migrate_checkpoint_namespacing --apply
    python -m backend.scripts.migrate_checkpoint_namespacing --apply --purge-ambiguous

Safe to re-run: already-namespaced rows are skipped.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from collections import defaultdict
from typing import Dict, List, Optional, Set

from backend.database import db_client, get_db
from backend.tenant.thread_scope import SEP, scoped_thread_id

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger("migrate_checkpoints")

COLLECTIONS = ("checkpoints", "writes")


async def _owners_by_thread() -> Dict[str, Set[str]]:
    """thread_id -> {tenant_id}, from every collection that records both."""
    db = get_db()
    owners: Dict[str, Set[str]] = defaultdict(set)

    async for doc in db.conversations.find({}, {"tenant_id": 1, "thread_id": 1}):
        tid, thread = doc.get("tenant_id"), doc.get("thread_id")
        if tid and thread:
            owners[str(thread)].add(str(tid))

    for coll, field in (("voice_call_links", "console_thread_id"),
                        ("voice_call_sessions", "console_thread_id")):
        async for doc in db[coll].find({}, {"tenant_id": 1, field: 1}):
            tid, thread = doc.get("tenant_id"), doc.get(field)
            if tid and thread:
                owners[str(thread)].add(str(tid))

    # vapi_<call_id> threads are recoverable from the call link
    async for doc in db.voice_call_links.find({}, {"tenant_id": 1, "call_id": 1}):
        tid, call_id = doc.get("tenant_id"), doc.get("call_id")
        if tid and call_id:
            owners[f"vapi_{call_id}"].add(str(tid))

    return owners


async def migrate(apply: bool, purge_ambiguous: bool) -> int:
    db = get_db()
    owners = await _owners_by_thread()
    logger.info("Recovered owners for %d thread ids", len(owners))

    stats = {"scanned": 0, "already": 0, "migrated": 0,
             "ambiguous": 0, "orphaned": 0, "purged": 0}
    ambiguous: List[str] = []
    orphaned: List[str] = []

    for coll_name in COLLECTIONS:
        coll = db[coll_name]
        thread_ids = await coll.distinct("thread_id")
        logger.info("%s: %d distinct thread ids", coll_name, len(thread_ids))

        for thread_id in thread_ids:
            if thread_id is None:
                continue
            thread_id = str(thread_id)
            stats["scanned"] += 1

            if SEP in thread_id:
                stats["already"] += 1
                continue

            candidates = owners.get(thread_id, set())

            if len(candidates) > 1:
                stats["ambiguous"] += 1
                ambiguous.append(f"{coll_name}:{thread_id} -> {sorted(candidates)}")
                if purge_ambiguous and apply:
                    res = await coll.delete_many({"thread_id": thread_id})
                    stats["purged"] += res.deleted_count
                continue

            if not candidates:
                stats["orphaned"] += 1
                orphaned.append(f"{coll_name}:{thread_id}")
                continue

            tenant_id = next(iter(candidates))
            new_key = scoped_thread_id(tenant_id, thread_id)
            n = await coll.count_documents({"thread_id": thread_id})
            if apply:
                await coll.update_many({"thread_id": thread_id},
                                       {"$set": {"thread_id": new_key}})
            stats["migrated"] += n
            logger.debug("%s: %s -> %s (%d docs)", coll_name, thread_id, new_key, n)

    verb = "Migrated" if apply else "Would migrate"
    logger.info("-" * 60)
    logger.info("%s %d checkpoint/write documents", verb, stats["migrated"])
    logger.info("Already namespaced : %d thread ids", stats["already"])
    logger.info("Ambiguous          : %d thread ids", stats["ambiguous"])
    logger.info("Orphaned (no owner): %d thread ids", stats["orphaned"])
    if purge_ambiguous:
        logger.info("Purged             : %d documents", stats["purged"])

    if ambiguous:
        logger.warning("Ambiguous thread ids (claimed by >1 tenant — this is the "
                       "cross-tenant collision T09 fixes):")
        for line in ambiguous[:20]:
            logger.warning("   %s", line)
        if len(ambiguous) > 20:
            logger.warning("   ... and %d more", len(ambiguous) - 20)
        logger.warning("Re-run with --purge-ambiguous to delete these; the affected "
                       "conversations will simply start with fresh agent memory.")

    if orphaned:
        logger.info("Orphaned thread ids (no conversation or voice record; likely "
                    "already-deleted threads): %d — left untouched.", len(orphaned))

    if not apply:
        logger.info("DRY RUN — nothing was written. Re-run with --apply.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (default is a dry run)")
    parser.add_argument("--purge-ambiguous", action="store_true",
                        help="delete checkpoints whose thread id is claimed by more than one tenant")
    args = parser.parse_args()

    db_client.connect()
    try:
        return asyncio.run(migrate(args.apply, args.purge_ambiguous))
    finally:
        db_client.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
