"""轻量词法记忆检索；中文双字词与英文标识符，无外部模型依赖。"""

from datetime import datetime
import math
import re

KINDS = {"preference", "fact", "experience"}
STATUSES = {"active", "candidate", "expired", "rejected"}
STOP_WORDS = {"项目", "这个", "那个", "一下", "什么", "如何", "使用", "请问", "帮我",
              "the", "a", "an", "and", "or", "to", "of", "is", "in", "for"}


def terms(text):
    result = set()
    for token in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text.casefold()):
        if "\u4e00" <= token[0] <= "\u9fff":
            result.update(token[index:index + 2] for index in range(len(token) - 1))
        elif len(token) > 1:
            result.add(token)
            result.update(part for part in token.split("_") if len(part) > 1)
    return result - STOP_WORDS


def active(item, now=None):
    if item.get("status", "active") != "active":
        return False
    expiration = item.get("expires_at")
    if expiration:
        deadline = datetime.fromisoformat(expiration)
        return deadline > (now or datetime.now().astimezone())
    return True


def rank_memories(items, query=None, *, include_inactive=False):
    eligible = [item for item in items if include_inactive or active(item)]
    if query is None:
        return [(item, 0.0) for item in reversed(eligible)]
    query_terms = terms(query)
    documents = [terms(item["text"]) for item in eligible]
    ranked = []
    for index, (item, document) in enumerate(zip(eligible, documents)):
        score = sum(1 + math.log((len(eligible) + 1) / (1 + sum(term in doc for doc in documents)))
                    for term in query_terms & document)
        # 偏好是始终适用的工作区背景；项目事实和经验要求词法相关。
        preference = item.get("kind", "fact") == "preference"
        if score or preference:
            ranked.append((item, score, preference, index))
    ranked.sort(key=lambda entry: (entry[2], entry[1], entry[3]), reverse=True)
    return [(item, score) for item, score, _, _ in ranked]


def check_sensitive_text(text):
    if re.search(r"\bsk-[A-Za-z0-9_-]{16,}|(?:api[_-]?key|password|密码|token)\s*[=:：]\s*[^\s]{8,}", text, re.I):
        raise ValueError("记忆疑似包含凭据，请脱敏后再保存")


def validate_metadata(kind, status, source, scope="project", expires_at=None):
    if (not isinstance(kind, str) or kind not in KINDS or not isinstance(status, str)
            or status not in STATUSES or scope != "project"):
        raise ValueError("记忆类型、状态或作用域不正确")
    if (not isinstance(source, dict) or not isinstance(source.get("type"), str)
            or source["type"] not in {"user", "task", "legacy"}):
        raise ValueError("记忆来源不正确")
    if source["type"] == "task":
        task_id = source.get("task_id")
        if (not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id)
                or source.get("verification") not in ("unverified", "passed", "failed")):
            raise ValueError("任务经验来源 ID 或验收状态不正确")
    if expires_at is not None:
        try:
            if datetime.fromisoformat(expires_at).tzinfo is None:
                raise ValueError("失效时间需要时区")
        except (TypeError, ValueError) as error:
            raise ValueError("记忆失效时间格式不正确") from error
