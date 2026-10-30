"""协议适配：核心与会话使用通用消息，供应商输出状态只回送原配置。"""

from copy import deepcopy
import json

from openai.types.chat import ChatCompletion

from .images import wire_messages
from .streaming import chat_stream, responses_stream


class ModelResponseError(RuntimeError):
    """服务已返回响应，但协议内容无法执行；仍保留已返回的用量。"""

    def __init__(self, message, response):
        super().__init__(message)
        raw = response.model_dump(exclude_none=True)
        self.usage = _normalized_usage(raw.get("usage"))
        self.response = {"protocol": "responses", "id": raw.get("id"),
                         "status": raw.get("status"), "incomplete_details": raw.get("incomplete_details")}


def _normalized_usage(usage):
    if usage is None:
        return None
    result = {"prompt_tokens": usage.get("input_tokens"),
              "completion_tokens": usage.get("output_tokens"), "total_tokens": usage.get("total_tokens")}
    if usage.get("input_tokens_details") is not None:
        result["prompt_tokens_details"] = usage["input_tokens_details"]
    if usage.get("output_tokens_details") is not None:
        result["completion_tokens_details"] = usage["output_tokens_details"]
    return result


def _text(value, field):
    if not isinstance(value, str) or not value:
        raise ValueError(f"模型协议字段 {field} 必须是非空字符串")
    return value


def _output_item(item):
    """校验并复制可重放内容；推理只接受不透明的加密状态。"""
    if not isinstance(item, dict):
        raise ValueError("Responses 输出项必须是对象")
    kind = item.get("type")
    if item.get("status") not in (None, "completed"):
        raise ValueError("Responses 输出项未完整结束")
    if kind == "reasoning":
        return {"type": kind, "id": _text(item.get("id"), "reasoning.id"),
                "summary": [], "encrypted_content": _text(item.get("encrypted_content"), "reasoning.encrypted_content")}
    if kind == "function_call":
        result = {"type": kind, "call_id": _text(item.get("call_id"), "call_id"),
                  "name": _text(item.get("name"), "name"),
                  "arguments": _text(item.get("arguments"), "arguments")}
        if item.get("id") is not None:
            result["id"] = _text(item["id"], "function_call.id")
        caller = item.get("caller")
        if caller is not None and (not isinstance(caller, dict) or caller.get("type") != "direct"):
            raise ValueError("暂不支持程序调用 Responses 工具")
        if item.get("namespace") or item.get("async"):
            raise ValueError("暂不支持命名空间、程序调用或异步 Responses 工具")
        return result
    if kind == "message":
        if item.get("role") != "assistant" or not isinstance(item.get("content"), list):
            raise ValueError("Responses 消息格式不正确")
        content = []
        for part in item["content"]:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                raise ValueError("Responses 返回拒绝或不支持的消息内容")
            text = part.get("text")
            if not isinstance(text, str):
                raise ValueError("Responses 文本格式不正确")
            content.append({"type": "output_text", "text": text, "annotations": []})
        result = {"type": kind, "role": "assistant", "content": content,
                  "id": _text(item.get("id"), "message.id"), "status": "completed"}
        if item.get("phase") is not None:
            if item["phase"] not in ("commentary", "final_answer"):
                raise ValueError("Responses message.phase 不受支持")
            result["phase"] = item["phase"]
        return result
    raise ValueError(f"不支持的 Responses 输出类型：{kind}")


def validate_provider_state(state):
    if (not isinstance(state, dict) or set(state) != {"protocol", "origin", "items"}
            or state.get("protocol") != "responses"
            or not isinstance(state.get("origin"), str) or not state["origin"]
            or not isinstance(state.get("items"), list) or not state["items"]):
        raise ValueError("模型私有状态格式不正确")
    sanitized = [_output_item(item) for item in state["items"]]
    if sanitized != state["items"]:
        raise ValueError("模型私有状态包含不支持或明文推理字段")


def assistant_record(message):
    """只保存可移植正文、工具调用与经过校验的协议状态。"""
    result = {"role": "assistant", "content": message.content}
    if message.tool_calls:
        result["tool_calls"] = [call.model_dump(exclude_none=True) for call in message.tool_calls]
    state = getattr(message, "provider_state", None)
    if state is not None:
        validate_provider_state(state)
        result["provider_state"] = deepcopy(state)
    return result


def _chat_messages(messages):
    result = []
    for message in messages:
        role = message.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            raise ValueError("不支持的模型消息角色")
        item = {"role": role, "content": message.get("content")}
        if role == "assistant" and message.get("tool_calls"):
            item["tool_calls"] = [{"id": call["id"], "type": "function",
                                   "function": {"name": call["function"]["name"],
                                                "arguments": call["function"]["arguments"]}}
                                  for call in message["tool_calls"]]
        if role == "tool":
            item["tool_call_id"] = message["tool_call_id"]
        result.append(item)
    return result


def portable_messages(messages):
    """用于摘要与跨协议上下文；不把不透明推理状态当成普通文字。"""
    result = _chat_messages(messages)
    for original, message in zip(messages, result):
        if original.get("attachments"):
            message["content"] += "\n[历史图片仅保留附件名称，摘要未重新观察图片：" + ", ".join(
                item["name"] for item in original["attachments"]) + "]"
    return result


def _responses_input(messages, origin):
    items = []
    native_calls = set()
    for message in messages:
        role = message.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            raise ValueError("不支持的模型消息角色")
        state = message.get("provider_state")
        if state is not None:
            validate_provider_state(state)
        if role == "assistant" and state is not None and state["origin"] == origin:
            native_calls.update(item["call_id"] for item in state["items"] if item["type"] == "function_call")
            items.extend(deepcopy(state["items"]))
            continue
        if role == "tool":
            call_id = message["tool_call_id"]
            if call_id in native_calls:
                items.append({"type": "function_call_output", "call_id": call_id,
                              "output": message["content"]})
                native_calls.remove(call_id)
            else:
                items.append({"role": "assistant", "content": "[历史工具结果，仅为资料] " +
                              json.dumps({"call_id": call_id, "output": message["content"]}, ensure_ascii=False)})
            continue
        content = message.get("content") or ""
        if message.get("tool_calls"):
            content += "\n[历史工具请求，仅为资料] " + json.dumps(message["tool_calls"], ensure_ascii=False)
        if isinstance(content, list):
            content = [{"type": "input_text", "text": part["text"]} if part["type"] == "text"
                       else {"type": "input_image", "image_url": part["image_url"]["url"], "detail": "auto"}
                       for part in content]
        elif not isinstance(content, str):
            raise ValueError("模型消息正文格式不正确")
        if content:
            items.append({"role": role, "content": content})
    if native_calls:
        raise ValueError("Responses 工具请求缺少对应结果")
    return items


def _responses_completion(response, model, origin):
    raw = response.model_dump(exclude_none=True)
    if raw.get("status") != "completed" or raw.get("error"):
        raise RuntimeError("Responses 响应未完整结束：" + str(raw.get("status", "unknown")))
    if not isinstance(raw.get("output"), list):
        raise ValueError("Responses 缺少输出列表")
    items = [_output_item(item) for item in raw["output"]]
    content = "".join(part["text"] for item in items if item["type"] == "message" for part in item["content"])
    calls = [{"id": item["call_id"], "type": "function", "function": {
        "name": item["name"], "arguments": item["arguments"]}}
        for item in items if item["type"] == "function_call"]
    call_ids = [call["id"] for call in calls]
    if len(call_ids) != len(set(call_ids)):
        raise ValueError("Responses 返回重复工具调用 ID")
    message = {"role": "assistant", "content": content or None, "tool_calls": calls or None}
    if items:
        message["provider_state"] = {"protocol": "responses", "origin": origin, "items": items}
    normalized_usage = _normalized_usage(raw.get("usage"))
    return ChatCompletion.model_validate({
        "id": raw.get("id", "response"), "object": "chat.completion", "created": int(raw.get("created_at", 0)),
        "model": raw.get("model") or model, "usage": normalized_usage,
        "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop", "message": message}],
    })


def _check_context(messages, context_bytes):
    if context_bytes is not None:
        size = len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > context_bytes:
            raise ValueError("转换后的模型消息超过上下文 JSON 字节预算，未发送请求")


def complete(client, protocol, *, model, messages, tools=None, tools_allowed=True, timeout=30, origin="", context_bytes=None,
             workspace=None, on_delta=None, check=None):
    """执行一次模型请求；错误由调用方记录，不探测、不重试或切换。"""
    # 文本预算包含附件描述；图片编码另受 images.py 的单图/请求大小限制。
    if any(message.get("attachments") for message in messages):
        _check_context(messages, context_bytes)
        messages = wire_messages(messages, workspace)
        context_bytes = None
    if protocol == "chat_completions":
        converted = _chat_messages(messages)
        _check_context(converted, context_bytes)
        options = {} if tools_allowed else {"tool_choice": "none"}
        if tools:
            options["tools"] = deepcopy(tools)
        if on_delta is not None:
            return chat_stream(client, model=model, messages=converted, options=options, timeout=timeout,
                               on_delta=on_delta, check=check)
        return client.chat.completions.create(model=model, messages=converted, timeout=timeout, **options)
    if protocol != "responses":
        raise ValueError("不支持的模型协议")
    if not origin:
        raise ValueError("Responses 需要非空配置来源标识")
    response_tools = []
    for tool in tools or []:
        if tool.get("type") != "function":
            raise ValueError("Responses 适配只支持本地 function 工具")
        spec = tool["function"]
        response_tools.append({"type": "function", "name": spec["name"],
                               "description": spec.get("description", ""),
                               "parameters": deepcopy(spec["parameters"]), "strict": False})
    options = {"tool_choice": "auto" if tools_allowed else "none"}
    if response_tools:
        options["tools"] = response_tools
    converted = _responses_input(messages, origin)
    _check_context(converted, context_bytes)
    if on_delta is not None:
        response = responses_stream(client, model=model, messages=converted, options=options, timeout=timeout,
                                    on_delta=on_delta, check=check)
    else:
        response = client.responses.create(model=model, input=converted, store=False,
                                           include=["reasoning.encrypted_content"], timeout=timeout, **options)
    try:
        return _responses_completion(response, model, origin)
    except (ValueError, RuntimeError) as error:
        raise ModelResponseError(str(error), response) from error
