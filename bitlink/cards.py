"""任务卡解析、字段校验与依赖图校验。

输入可以是顶层数组，也可以是 `{"cards": [...]}`。任何一张卡不合法都整批拒绝，
错误信息带 1 基卡序号，调用方据此返回退出码 2 且不落库。
"""

import json
import re
import uuid

REQUIRED_FIELDS = ("title", "description", "acceptance")
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
ALLOWED_FIELDS = {
    "id",
    "title",
    "description",
    "acceptance",
    "blocked_by",
    "max_attempts",
    "timeout_seconds",
}
DEFAULT_MAX_ATTEMPTS = 3

_CARD_TEMPLATE = {"title", "description", "acceptance"}


class CardError(ValueError):
    """任务卡输入不合法。"""


def _is_positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _extract_cards(payload):
    if isinstance(payload, dict):
        if "cards" not in payload:
            raise CardError("输入必须是卡片数组，或包含 cards 字段的对象")
        cards = payload["cards"]
    else:
        cards = payload
    if not isinstance(cards, list) or not cards:
        raise CardError("cards 必须是非空数组")
    return cards


def _validate_card(index, raw, existing_ids, batch_ids):
    if not isinstance(raw, dict):
        raise CardError(f"第 {index} 张卡必须是对象")

    unknown = set(raw) - ALLOWED_FIELDS
    if unknown:
        raise CardError(f"第 {index} 张卡包含未知字段：{sorted(unknown)}")

    for field in REQUIRED_FIELDS:
        if field not in raw:
            raise CardError(f"第 {index} 张卡缺少必填字段 {field}")
        if not isinstance(raw[field], str):
            raise CardError(f"第 {index} 张卡的 {field} 必须是字符串")
    for field in ("title", "acceptance"):
        if not raw[field].strip():
            raise CardError(f"第 {index} 张卡的 {field} 不能为空")

    card_id = raw.get("id")
    if card_id is not None:
        if not isinstance(card_id, str) or not card_id.strip():
            raise CardError(f"第 {index} 张卡的 id 必须是非空字符串")
        if not ID_PATTERN.fullmatch(card_id):
            raise CardError(
                f"第 {index} 张卡的 id 只能包含字母、数字、下划线与连字符，"
                f"长度 1-64，且不能含路径分隔符：{card_id!r}"
            )
        if card_id in existing_ids:
            raise CardError(f"第 {index} 张卡的 id 已存在于数据库：{card_id}")
        if card_id in batch_ids:
            raise CardError(f"第 {index} 张卡的 id 与同批次重复：{card_id}")
    else:
        card_id = uuid.uuid4().hex

    blocked_by = raw.get("blocked_by", [])
    if blocked_by is None:
        blocked_by = []
    if not isinstance(blocked_by, list):
        raise CardError(f"第 {index} 张卡的 blocked_by 必须是数组")
    normalized = []
    for dependency in blocked_by:
        if not isinstance(dependency, str) or not dependency.strip():
            raise CardError(f"第 {index} 张卡的 blocked_by 元素必须是非空字符串")
        if dependency not in normalized:
            normalized.append(dependency)

    max_attempts = raw.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
    if not _is_positive_int(max_attempts):
        raise CardError(f"第 {index} 张卡的 max_attempts 必须是 >= 1 的整数")

    timeout_seconds = raw.get("timeout_seconds")
    if timeout_seconds is not None and not _is_positive_int(timeout_seconds):
        raise CardError(f"第 {index} 张卡的 timeout_seconds 必须是 >= 1 的整数或 null")

    return {
        "id": card_id,
        "title": raw["title"].strip(),
        "description": raw["description"],
        "acceptance": raw["acceptance"],
        "blocked_by": normalized,
        "max_attempts": max_attempts,
        "timeout_seconds": timeout_seconds,
    }


def _check_cycles(cards):
    adjacency = {card["id"]: [d for d in card["blocked_by"] if d in {c["id"] for c in cards}] for card in cards}
    state = {}

    def visit(node, trail):
        marker = state.get(node)
        if marker == "done":
            return
        if marker == "visiting":
            cycle = " -> ".join(trail + [node])
            raise CardError(f"blocked_by 依赖成环：{cycle}")
        state[node] = "visiting"
        for dependency in adjacency.get(node, []):
            visit(dependency, trail + [node])
        state[node] = "done"

    for card in cards:
        visit(card["id"], [])


def parse_cards(payload, existing_ids=()):
    """校验并规范化卡片，返回带 id 的字典列表；不合法抛 CardError。"""
    existing = set(existing_ids)
    raw_cards = _extract_cards(payload)

    cards = []
    batch_ids = set()
    for index, raw in enumerate(raw_cards, start=1):
        card = _validate_card(index, raw, existing, batch_ids)
        cards.append(card)
        batch_ids.add(card["id"])

    known = existing | batch_ids
    for index, card in enumerate(cards, start=1):
        for dependency in card["blocked_by"]:
            if dependency == card["id"]:
                raise CardError(f"第 {index} 张卡的 blocked_by 不允许引用自身")
            if dependency not in known:
                raise CardError(f"第 {index} 张卡依赖的任务不存在：{dependency}")

    _check_cycles(cards)
    return cards


def load_cards_file(path, existing_ids=()):
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise CardError(f"无法读取卡片文件：{path}（{exc}）") from exc
    except json.JSONDecodeError as exc:
        raise CardError(f"卡片文件不是合法 JSON：{path}（{exc}）") from exc
    return parse_cards(payload, existing_ids=existing_ids)
