"""增量读取模型输出；只有完整收齐工具参数后才返回可执行的响应。"""

from openai.types.chat import ChatCompletion

MAX_STREAM_BYTES = 1024 * 1024


class IncompleteStreamError(RuntimeError):
    def __init__(self, message, *, protocol, usage=None, partial_text=""):
        super().__init__(message)
        self.usage = usage
        self.response = {"protocol": protocol, "status": "interrupted", "partial_text": partial_text}


def chat_stream(client, *, model, messages, options, timeout, on_delta, check):
    texts, calls = [], {}
    usage, finish = None, None
    identifier, created, response_model = "stream", 0, model
    received = 0
    with client.chat.completions.create(model=model, messages=messages, timeout=timeout,
                                        stream=True, stream_options={"include_usage": True}, **options) as stream:
        for chunk in stream:
            if check:
                check()
            identifier, created, response_model = chunk.id, chunk.created, chunk.model
            if chunk.usage is not None:
                usage = chunk.usage.model_dump(exclude_none=True)
            for choice in chunk.choices:
                if choice.index != 0:
                    raise ValueError("流式输出只支持一个候选回答")
                delta = choice.delta
                if delta.content:
                    if finish is not None:
                        raise ValueError("模型在结束标记后继续输出正文")
                    texts.append(delta.content)
                    received += len(delta.content.encode("utf-8"))
                    if on_delta:
                        on_delta(delta.content)
                if delta.refusal:
                    raise IncompleteStreamError("模型拒绝了本次请求", protocol="chat_completions", usage=usage,
                                                partial_text="".join(texts))
                for part in delta.tool_calls or []:
                    if finish is not None or not 0 <= part.index < 32:
                        raise ValueError("流式工具调用的顺序或数量不正确")
                    target = calls.setdefault(part.index, {"id": "", "type": "function",
                                                           "function": {"name": "", "arguments": ""}})
                    if part.type not in (None, "function"):
                        raise ValueError("不支持的流式工具类型")
                    if part.id:
                        target["id"] += part.id
                    if part.function:
                        for field in ("name", "arguments"):
                            fragment = getattr(part.function, field)
                            if fragment:
                                target["function"][field] += fragment
                                received += len(fragment.encode("utf-8"))
                if choice.finish_reason is not None:
                    if finish is not None and finish != choice.finish_reason:
                        raise ValueError("模型返回了冲突的结束标记")
                    finish = choice.finish_reason
            if received > MAX_STREAM_BYTES:
                raise IncompleteStreamError("模型流式输出超过 1 MiB", protocol="chat_completions", usage=usage,
                                            partial_text="".join(texts))
    if finish is None:
        raise IncompleteStreamError("模型流式连接结束，但缺少完成标记；未执行工具", protocol="chat_completions",
                                    usage=usage, partial_text="".join(texts))
    if calls and sorted(calls) != list(range(len(calls))):
        raise ValueError("流式工具调用序号不连续")
    return ChatCompletion.model_validate({
        "id": identifier, "object": "chat.completion", "created": created, "model": response_model,
        "usage": usage, "choices": [{"index": 0, "finish_reason": finish, "message": {
            "role": "assistant", "content": "".join(texts) or None,
            "tool_calls": [calls[index] for index in sorted(calls)] or None,
        }}],
    })


def responses_stream(client, *, model, messages, options, timeout, on_delta, check):
    response, texts, received = None, [], 0
    with client.responses.create(model=model, input=messages, timeout=timeout, store=False,
                                  include=["reasoning.encrypted_content"], stream=True, **options) as stream:
        for event in stream:
            if check:
                check()
            if event.type == "response.output_text.delta":
                texts.append(event.delta)
                received += len(event.delta.encode("utf-8"))
                if on_delta:
                    on_delta(event.delta)
            elif event.type in {"response.completed", "response.incomplete", "response.failed"}:
                response = event.response
            elif event.type == "error":
                raise IncompleteStreamError("Responses 流式请求失败", protocol="responses", partial_text="".join(texts))
            if received > MAX_STREAM_BYTES:
                raise IncompleteStreamError("模型流式输出超过 1 MiB", protocol="responses", partial_text="".join(texts))
    if response is None:
        raise IncompleteStreamError("Responses 流式连接结束，但缺少完成事件；未执行工具", protocol="responses",
                                    partial_text="".join(texts))
    return response
