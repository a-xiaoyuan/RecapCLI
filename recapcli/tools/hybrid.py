"""FTS、文件/符号名称与可选向量的排名融合，不把关键词检索冒充语义检索。"""

import json
from heapq import nlargest
import re
import time

from openai import APIError

from ..llm.embeddings import EmbeddingClient


MAX_EMBED_CHUNKS = 256


def rank_rows(connection, query, limit, timeout=20):
    tokens = list(dict.fromkeys(re.findall(r"[\w]+", query.casefold())))[:16]
    split = list(dict.fromkeys(part for token in tokens for part in token.split("_") if part))
    fts_query = " OR ".join('"' + term.replace('"', '""') + '"' for term in [query, *split])
    lexical = connection.execute("""SELECT lines.path, CAST(line AS INTEGER), CAST(line AS INTEGER), text, files.digest
        FROM lines JOIN files ON files.path=lines.path WHERE lines MATCH ? AND in_scope(lines.path)
        ORDER BY bm25(lines), lines.path, CAST(line AS INTEGER) LIMIT ?""", (fts_query, limit)).fetchall()
    named = []
    if split:
        clause = " OR ".join("lower(symbols.path || ' ' || name) LIKE ?" for _ in split)
        patterns = ["%" + token + "%" for token in split]
        named = connection.execute(f"""SELECT symbols.path,start_line,min(end_line,start_line+39),name,files.digest
            FROM symbols JOIN files ON files.path=symbols.path WHERE ({clause}) AND in_scope(symbols.path)
            ORDER BY symbols.path,start_line LIMIT ?""", (*patterns, limit)).fetchall()
        clause = " OR ".join("lower(path) LIKE ?" for _ in split)
        named.extend(connection.execute(f"""SELECT path,start_line,end_line,text,digest FROM chunks
            WHERE ({clause}) AND in_scope(path) ORDER BY path,start_line LIMIT ?""", (*patterns, limit)).fetchall())
    metadata = {"semantic_status": "disabled", "semantic_note": "未配置 Embedding；当前仅融合关键词与文件/符号名称"}
    semantic = []
    started = time.monotonic()
    try:
        client = EmbeddingClient.configured(timeout)
        if client is not None:
            join = """FROM chunks c LEFT JOIN vectors v ON v.path=c.path AND v.start_line=c.start_line
                AND v.model=? AND v.digest=c.digest WHERE v.path IS NULL AND in_scope(c.path)"""
            count = connection.execute("SELECT count(*) " + join, (client.identity,)).fetchone()[0]
            missing = connection.execute("SELECT c.path,c.start_line,c.end_line,c.text,c.digest " + join
                                         + " ORDER BY c.path,c.start_line LIMIT ?",
                                         (client.identity, MAX_EMBED_CHUNKS)).fetchall()
            pending = max(0, count - MAX_EMBED_CHUNKS)
            # 先结束扫描事务，网络等待期间不占 SQLite 写锁。
            connection.commit()
            for offset in range(0, min(len(missing), MAX_EMBED_CHUNKS), 16):
                client.timeout = min(20, timeout - (time.monotonic() - started))
                if client.timeout <= 0:
                    raise TimeoutError("Embedding 时间预算耗尽")
                batch = missing[offset:offset + min(16, MAX_EMBED_CHUNKS - offset)]
                vectors = client.embed([row[0] + "\n" + row[3] for row in batch])
                with connection:
                    for row, vector in zip(batch, vectors):
                        connection.execute("INSERT OR REPLACE INTO vectors VALUES (?,?,?,?,?)",
                                           (row[0], row[1], client.identity, row[4], json.dumps(vector)))
            client.timeout = min(20, timeout - (time.monotonic() - started))
            if client.timeout <= 0:
                raise TimeoutError("Embedding 时间预算耗尽")
            target = client.embed([query])[0]
            def scored_rows():
                rows = connection.execute("""SELECT c.path,c.start_line,c.end_line,c.text,c.digest,v.vector
                    FROM chunks c JOIN vectors v ON v.path=c.path AND v.start_line=c.start_line
                    AND v.digest=c.digest WHERE v.model=? AND in_scope(c.path) ORDER BY c.path,c.start_line""", (client.identity,))
                for row in rows:
                    vector = json.loads(row[-1])
                    if len(vector) == len(target):
                        score = sum(a * b for a, b in zip(vector, target))
                        if score > 0:
                            yield score, row[:-1]
            semantic = [row for _, row in nlargest(limit, scored_rows(), key=lambda item: item[0])]
            metadata = {"semantic_status": "partial" if pending else "enabled", "pending_embedding_chunks": pending,
                        "semantic_note": "向量仅作为候选召回，仍需读取源码核实；未建立向量的分块不会被语义召回"}
    except (APIError, OSError, ValueError) as error:
        metadata = {"semantic_status": "unavailable", "semantic_note": f"Embedding 不可用（{type(error).__name__}），已回退本地检索"}

    fused = {}
    for source, rows in (("keyword", lexical), ("name", named[:limit]), ("semantic", semantic)):
        seen = set()
        for rank, row in enumerate(rows, 1):
            # 不同通道命中同一代码分块时合并，优先保留关键词的精确行定位。
            key = (row[0], (row[1] - 1) // 40)
            if key in seen:
                continue
            seen.add(key)
            item = fused.setdefault(key, {"row": row, "score": 0, "sources": []})
            item["score"] += 1 / (60 + rank)
            item["sources"].append(source)
    ranked = sorted(fused.values(), key=lambda item: (-item["score"], item["row"][0], item["row"][1]))[:limit]
    scores = {tuple(item["row"][:3]): {"score": round(item["score"], 6), "sources": item["sources"]} for item in ranked}
    metadata["candidate_truncated"] = any(len(rows) >= limit for rows in (lexical, named, semantic))
    return [item["row"] for item in ranked], scores, metadata
