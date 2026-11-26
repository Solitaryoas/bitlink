"""执行报告契约：字段别名、类型规范化和合法性判定。

daemon 用它校验磁盘报告，执行器用它判断 `-o` 产物是不是一份合规报告，
两边共用，避免「能解析」被当成「是报告」。
"""

import json

REPORT_FIELDS = ("status", "summary", "evidence", "files_changed", "blockers")
STATUSES = ("completed", "failed", "blocked")
REPORT_ALIASES = {"changes": "files_changed", "verification": "evidence"}


def _as_text_item(item):
    if isinstance(item, str):
        return item
    return json.dumps(item, ensure_ascii=False)


def normalize_report(payload):
    """把模型输出规范成契约要求的五个字段；不可修复时返回 None。"""
    if not isinstance(payload, dict):
        return None
    payload = dict(payload)
    for alias, target in REPORT_ALIASES.items():
        if target not in payload and alias in payload:
            payload[target] = payload[alias]
        payload.pop(alias, None)
    payload.setdefault("files_changed", [])
    payload.setdefault("blockers", [])
    if payload.get("status") not in STATUSES:
        return None
    if not isinstance(payload.get("summary"), str):
        return None
    for key in ("evidence", "files_changed", "blockers"):
        value = payload.get(key)
        if not isinstance(value, list):
            return None
        payload[key] = [_as_text_item(item) for item in value if str(item).strip()]
    return payload


def has_evidence(payload):
    return bool(payload.get("evidence"))
