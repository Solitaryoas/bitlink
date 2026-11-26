"""从模型输出里提取 JSON 对象：兼容 markdown 围栏、前后解释和多对象并存。

当前使用的模型不一定支持 --output-schema 强制结构化输出，最终消息可能是
「一段说明 + 一个 JSON 对象」或带 ```json 围栏的文本，因此不能直接 json.load。
"""

import json
import re


def iter_json_objects(text):
    """按出现顺序产出文本里所有可解析的 JSON 对象。

    模型经常在字符串值里直接写未转义的引号（例如解释代码时说
    `使用 json.dumps，{"tasks": []}`），这种文本不是合法 JSON，
    因此解析失败后会用启发式修复再试一次。
    """
    if not text:
        return
    for payload in _objects_in(text):
        yield payload
    repaired = _escape_inner_quotes(text)
    if repaired != text:
        for payload in _objects_in(repaired):
            yield payload


def _objects_in(text):
    stripped = text.strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        yield payload
        return

    for fenced in re.findall(r"```(?:json)?\s*(.*?)```", stripped, re.S):
        for payload in _balanced_objects(fenced):
            yield payload
    for payload in _balanced_objects(stripped):
        yield payload


def _escape_inner_quotes(text):
    """把字符串值里未转义的 `"` 变成 `\\"`。

    判据：字符串内部遇到的引号，若其后第一个非空白字符不是 `,` `:` `}` `]`，
    就当作内容而不是闭合引号。只在 json.loads 已经失败时使用。
    """
    out = []
    in_string = False
    escaped = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if not in_string:
            if char == '"':
                in_string = True
            out.append(char)
            index += 1
            continue
        if escaped:
            escaped = False
            out.append(char)
            index += 1
            continue
        if char == "\\":
            escaped = True
            out.append(char)
            index += 1
            continue
        if char == '"':
            lookahead = index + 1
            while lookahead < length and text[lookahead] in " \t\r\n":
                lookahead += 1
            following = text[lookahead] if lookahead < length else ""
            if following in (",", ":", "}", "]", ""):
                in_string = False
                out.append(char)
            else:
                out.append('\\"')
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def extract_json_object(text):
    """返回文本中第一个可解析的 JSON 对象；找不到返回 None。"""
    return next(iter_json_objects(text), None)


def find_json_object(text, predicate):
    """返回第一个满足 predicate 的 JSON 对象；用于跳过回显的任务卡等无关对象。"""
    for payload in iter_json_objects(text):
        if predicate(payload):
            return payload
    return None


def _balanced_objects(text):
    start = text.find("{")
    while start != -1:
        payload = _parse_from(text, start)
        if payload is not None:
            yield payload
        start = text.find("{", start + 1)


def _parse_from(text, start):
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                chunk = text[start : index + 1]
                try:
                    payload = json.loads(chunk)
                except json.JSONDecodeError:
                    return None
                return payload if isinstance(payload, dict) else None
    return None
