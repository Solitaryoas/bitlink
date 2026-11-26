"""从模型输出里提取 JSON 对象：兼容 markdown 围栏、前后解释和多对象并存。

当前使用的模型不一定支持 --output-schema 强制结构化输出，最终消息可能是
「一段说明 + 一个 JSON 对象」或带 ```json 围栏的文本，因此不能直接 json.load。
"""

import json
import re


def iter_json_objects(text):
    """按出现顺序产出文本里所有可解析的 JSON 对象。"""
    if not text:
        return
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
