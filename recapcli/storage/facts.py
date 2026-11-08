"""项目事实的长期记忆：SQLite 保存词项，按查询词重合度召回。"""

from contextlib import contextmanager
from datetime import datetime
import sqlite3
import uuid
from pathlib import Path

from ..base.files import state_directory
from ..core.memory import check_sensitive_text, terms

MAX_FACT_TEXT = 2000


def fact_database(workspace):
    return state_directory(workspace, "facts") / "facts.sqlite3"


def short_term_messages(session):
    """短期记忆就是当前会话里已经落盘的对话，不包含其他会话。"""
    messages = session.get("messages") if isinstance(session, dict) else None
    if not isinstance(messages, list):
        return []
    return [message for message in messages if message.get("role") in {"user", "assistant", "tool"}]


class FactStore:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.path = fact_database(self.workspace)
        self._ensure()

    @contextmanager
    def _connect(self):
        # sqlite3 的 with 只提交事务、不关连接；Windows 上未关闭的连接会锁住文件。
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA temp_store=MEMORY")
            with connection:
                yield connection
        finally:
            connection.close()

    def _ensure(self):
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS facts (id TEXT PRIMARY KEY, text TEXT NOT NULL, created_at TEXT NOT NULL)")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS fact_terms (fact_id TEXT NOT NULL, term TEXT NOT NULL)")
            connection.execute("CREATE INDEX IF NOT EXISTS fact_terms_term ON fact_terms(term)")

    def add(self, text):
        if not isinstance(text, str):
            raise ValueError("事实内容必须是字符串")
        text = text.strip()
        if not text:
            raise ValueError("事实内容不能为空")
        if len(text) > MAX_FACT_TEXT:
            raise ValueError(f"单条事实不能超过 {MAX_FACT_TEXT} 字")
        check_sensitive_text(text)
        normalized = " ".join(text.split())
        with self._connect() as connection:
            existing = connection.execute("SELECT id, text, created_at FROM facts WHERE text = ?",
                                          (normalized,)).fetchone()
            if existing is not None:
                return {"id": existing["id"], "text": existing["text"], "created_at": existing["created_at"]}
            fact_id = "f" + uuid.uuid4().hex[:8]
            created_at = datetime.now().astimezone().isoformat(timespec="seconds")
            connection.execute("INSERT INTO facts (id, text, created_at) VALUES (?, ?, ?)",
                               (fact_id, normalized, created_at))
            connection.executemany("INSERT INTO fact_terms (fact_id, term) VALUES (?, ?)",
                                   [(fact_id, term) for term in sorted(terms(normalized))])
        return {"id": fact_id, "text": normalized, "created_at": created_at}

    def count(self):
        with self._connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0]

    def recall(self, query, k=3):
        if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 20:
            raise ValueError("召回条数必须是 1 到 20 的整数")
        if query is not None and not isinstance(query, str):
            raise ValueError("召回查询必须是字符串")
        query_terms = sorted(terms(query or ""))
        with self._connect() as connection:
            if not query_terms:
                rows = connection.execute(
                    "SELECT id, text FROM facts ORDER BY created_at DESC, id DESC LIMIT ?", (k,)).fetchall()
                return [{"id": row["id"], "text": row["text"], "score": 0} for row in rows]
            placeholders = ",".join("?" * len(query_terms))
            rows = connection.execute(
                f"""SELECT f.id, f.text, COUNT(*) AS score
                    FROM facts f JOIN fact_terms t ON t.fact_id = f.id
                    WHERE t.term IN ({placeholders})
                    GROUP BY f.id
                    ORDER BY score DESC, f.created_at DESC, f.id DESC
                    LIMIT ?""",
                (*query_terms, k)).fetchall()
        return [{"id": row["id"], "text": row["text"], "score": row["score"]} for row in rows]
