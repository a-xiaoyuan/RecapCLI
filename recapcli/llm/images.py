"""用户显式引用的本地图片；存档只保留附件描述，发送时校验并编码。"""

import base64
from copy import deepcopy
import hashlib
import io
from pathlib import Path
import re
import warnings

from PIL import Image, UnidentifiedImageError

from ..storage.files import data_root, state_directory

MAX_IMAGES = 4
MAX_SOURCE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_REQUEST_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 25_000_000
MIME_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}
REFERENCE = re.compile(r'@image:(?:<([^>]+)>|"([^"]+)"|\'([^\']+)\'|([^\s<>，。；！？]+))')


def has_image_references(prompt):
    return "@image:" in prompt


def validate_attachments(attachments):
    if not isinstance(attachments, list) or not 1 <= len(attachments) <= MAX_IMAGES:
        raise ValueError("图片附件必须为 1 到 4 项")
    for item in attachments:
        if (not isinstance(item, dict) or set(item) != {"sha256", "mime_type", "size", "name"}
                or not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                or item["mime_type"] not in MIME_TYPES.values()
                or isinstance(item["size"], bool) or not isinstance(item["size"], int)
                or not 0 < item["size"] <= MAX_IMAGE_BYTES
                or not isinstance(item["name"], str) or not 1 <= len(item["name"]) <= 255
                or any(char in item["name"] for char in "\r\n/\\")):
            raise ValueError("图片附件描述不正确")


def _image_bytes(path):
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_SOURCE_BYTES:
        raise ValueError("图片不存在、为空或超过 10 MiB")
    with path.open("rb") as source:
        raw = source.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("图片超过 10 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in MIME_TYPES or image.width * image.height > MAX_PIXELS:
                    raise ValueError("只支持 PNG、JPEG、WebP、静态 GIF，图片最多 2500 万像素")
                if getattr(image, "is_animated", False):
                    raise ValueError("当前仅支持静态图片")
                image.load()
                image.thumbnail((2048, 2048))
                output = io.BytesIO()
                if "A" in image.getbands() or image.mode == "P":
                    image.convert("RGBA").save(output, format="PNG", optimize=True)
                    mime = "image/png"
                else:
                    image.convert("RGB").save(output, format="JPEG", quality=85, optimize=True)
                    mime = "image/jpeg"
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning, OSError) as error:
        raise ValueError("图片格式无效或无法解码") from error
    data = output.getvalue()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("处理后的图片超过 4 MiB，请缩小图片")
    return data, mime


def prepare_user_message(prompt, workspace):
    """只解析当前用户输入，不解析模型输出、工具结果或历史文字。"""
    matches = list(REFERENCE.finditer(prompt))
    if not matches:
        if has_image_references(prompt):
            raise ValueError("图片用法：@image:./shot.png；路径含空格时使用 @image:<路径>")
        return {"role": "user", "content": prompt}
    if len(matches) > MAX_IMAGES:
        raise ValueError("一次最多附加 4 张图片")
    if "@image:" in REFERENCE.sub("", prompt):
        raise ValueError("存在不完整的 @image: 图片引用")
    workspace = Path(workspace).resolve()
    payloads = []
    for match in matches:
        reference = next(value for value in match.groups() if value is not None)
        target = Path(reference)
        if not target.is_absolute():
            target = workspace / target
        if not target.absolute().is_relative_to(workspace):
            raise ValueError("图片必须位于当前工作区内")
        cursor = workspace
        for part in target.absolute().relative_to(workspace).parts:
            cursor /= part
            if cursor.is_symlink() or cursor.is_junction():
                raise ValueError("图片路径不能经过链接")
        # 图片不跟随链接；凭据和运行数据不能借助图片输入读取。
        from ..tools.filesystem import _checked_target, SKIP_DIRS
        target = _checked_target(workspace, target)
        relative = target.relative_to(workspace)
        if any(part in SKIP_DIRS for part in relative.parts):
            raise ValueError("不能把凭据或运行数据作为图片附件")
        data, mime = _image_bytes(target)
        payloads.append((data, {"sha256": hashlib.sha256(data).hexdigest(), "mime_type": mime,
                                "size": len(data), "name": target.name}))
    if sum(len(data) for data, _ in payloads) > MAX_REQUEST_IMAGE_BYTES:
        raise ValueError("本轮图片附件总大小超过 8 MiB")
    directory = state_directory(workspace, "attachments")
    for data, item in payloads:
        path = directory / item["sha256"]
        if path.is_symlink() or path.is_junction():
            raise ValueError("图片附件不能是链接")
        if not path.exists():
            with path.open("xb") as output:
                output.write(data)
        elif path.read_bytes() != data:
            raise ValueError("已有图片附件校验失败")
    text = REFERENCE.sub(lambda match: "[图片：" + Path(next(value for value in match.groups() if value is not None)).name + "]", prompt)
    return {"role": "user", "content": text, "attachments": [item for _, item in payloads]}


def image_data_url(item, workspace):
    validate_attachments([item])
    if workspace is None:
        raise ValueError("读取图片附件需要指定工作区")
    workspace = Path(workspace).resolve()
    root = data_root(workspace)
    path = root / "attachments" / item["sha256"]
    for candidate in (root, path.parent, path):
        if candidate.is_symlink() or candidate.is_junction() or not candidate.resolve().is_relative_to(workspace):
            raise ValueError("图片附件路径不正确")
    if not path.is_file() or path.stat().st_size != item["size"]:
        raise ValueError("图片附件缺失或大小变化，请重新附加图片")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise ValueError("图片附件校验失败，请重新附加图片")
    return "data:" + item["mime_type"] + ";base64," + base64.b64encode(data).decode("ascii")


def wire_messages(messages, workspace):
    """仅在请求边界展开图片，不把编码写入会话、摘要或执行追溯。"""
    result = deepcopy(messages)
    total = 0
    for message in result:
        attachments = message.pop("attachments", None)
        if attachments is None:
            continue
        if message.get("role") != "user":
            raise ValueError("只有用户消息可以携带图片")
        validate_attachments(attachments)
        total += sum(item["size"] for item in attachments)
        if total > MAX_REQUEST_IMAGE_BYTES:
            raise ValueError("所选上下文的图片超过 8 MiB，请新建会话或压缩旧历史")
        message["content"] = [{"type": "text", "text": message["content"]}, *[
            {"type": "image_url", "image_url": {"url": image_data_url(item, workspace)}} for item in attachments
        ]]
    return result
