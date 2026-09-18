# Declarative card block builders -- the Slack Block Kit-style vocabulary
# validated server-side by salt-api's app/models/card.rb (CARD_PROTOCOL_SPEC.md
# L1). These functions build plain dicts matching that model's
# `validate_one_block`/`validate_actions` exactly: `section`, `image`,
# `divider`, and `actions` (button rows), including `action_type: "pay"` and
# `action_type: "handoff"` buttons that the SERVER dispatches on by TYPE,
# never by label. The limits below (MAX_BLOCKS, MAX_LABEL, ...) mirror that
# model's constants so a bad card fails fast, client-side, with the same
# message shape the server would give.
from __future__ import annotations

from typing import Any, Literal

MAX_BLOCKS = 20
MAX_BUTTONS_PER_ACTIONS = 5
MAX_FIELDS = 10
MAX_TEXT = 2000
MAX_LABEL = 40
MAX_FIELD_VALUE = MAX_LABEL * 4
MAX_URL = 500
MAX_RESTRICTED_TO = 20

ButtonStyle = Literal["primary", "danger"]
ActionType = Literal["default", "pay", "handoff"]


def field(label: str, value: str) -> dict[str, str]:
    """One entry of a `section` block's `fields` array."""
    if len(label) > MAX_LABEL:
        raise ValueError(f"field label must be <= {MAX_LABEL} chars")
    if len(value) > MAX_FIELD_VALUE:
        raise ValueError(f"field value must be <= {MAX_FIELD_VALUE} chars")
    return {"label": label, "value": value}


def section(text: str | None = None, fields: list[dict[str, str]] | None = None) -> dict[str, Any]:
    if text is None and fields is None:
        raise ValueError("section needs text and/or fields")
    if text is not None and len(text) > MAX_TEXT:
        raise ValueError(f"section text must be <= {MAX_TEXT} chars")
    if fields is not None and len(fields) > MAX_FIELDS:
        raise ValueError(f"section fields must be <= {MAX_FIELDS} entries")
    block: dict[str, Any] = {"type": "section"}
    if text is not None:
        block["text"] = text
    if fields is not None:
        block["fields"] = fields
    return block


def divider() -> dict[str, Any]:
    return {"type": "divider"}


def image(url: str, alt: str | None = None) -> dict[str, Any]:
    if len(url) > MAX_URL or not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError(f"image url must be an http(s) URL <= {MAX_URL} chars")
    block: dict[str, Any] = {"type": "image", "url": url}
    if alt is not None:
        if len(alt) > MAX_FIELD_VALUE:
            raise ValueError(f"image alt must be <= {MAX_FIELD_VALUE} chars")
        block["alt"] = alt
    return block


def button(
    action_id: str,
    label: str,
    *,
    style: ButtonStyle | None = None,
    action_type: ActionType | None = None,
    pay: dict[str, str] | None = None,
    handoff_target_agent_id: str | None = None,
    restricted_to: list[str] | None = None,
) -> dict[str, Any]:
    """A single button element inside an `actions` block.

    `action_type` is what salt-api dispatches on, server-side, never the
    label: "pay" needs `pay={"amount": "<human decimal string>", "currency": ...}`
    (e.g. `{"amount": "1.50", "currency": "USDC"}` -- NEVER base units/wei,
    matching card.rb's Product::AMOUNT_RE check) and becomes a real
    TransferRequest on the one payment rail; "handoff" needs
    `handoff_target_agent_id` and performs a real hand-off when tapped.
    """
    if not (1 <= len(action_id) <= 40):
        raise ValueError("action_id must be 1..40 chars")
    if not (1 <= len(label) <= MAX_LABEL):
        raise ValueError(f"label must be 1..{MAX_LABEL} chars")
    if style is not None and style not in ("primary", "danger"):
        raise ValueError("style must be 'primary' or 'danger'")

    btn: dict[str, Any] = {"type": "button", "action_id": action_id, "label": label}
    if style is not None:
        btn["style"] = style

    if action_type is None or action_type == "default":
        pass
    elif action_type == "pay":
        if not pay or "amount" not in pay or "currency" not in pay:
            raise ValueError('pay buttons need pay={"amount": "<decimal, e.g. 1.50>", "currency": ...}')
        btn["action_type"] = "pay"
        btn["pay"] = {"amount": str(pay["amount"]), "currency": str(pay["currency"])}
    elif action_type == "handoff":
        if not handoff_target_agent_id:
            raise ValueError("handoff buttons need handoff_target_agent_id")
        btn["action_type"] = "handoff"
        btn["handoff_target_agent_id"] = handoff_target_agent_id
    else:
        raise ValueError("action_type must be 'default', 'pay', or 'handoff'")

    if restricted_to is not None:
        if not (1 <= len(restricted_to) <= MAX_RESTRICTED_TO):
            raise ValueError(f"restricted_to must have 1..{MAX_RESTRICTED_TO} entries")
        btn["restricted_to"] = list(restricted_to)

    return btn


def pay_button(
    action_id: str,
    label: str,
    *,
    amount: str,
    currency: str,
    style: ButtonStyle | None = None,
    restricted_to: list[str] | None = None,
) -> dict[str, Any]:
    """Convenience wrapper: `button(..., action_type="pay", pay={...})`."""
    return button(
        action_id,
        label,
        style=style,
        action_type="pay",
        pay={"amount": amount, "currency": currency},
        restricted_to=restricted_to,
    )


def handoff_button(
    action_id: str,
    label: str,
    *,
    target_agent_id: str,
    style: ButtonStyle | None = None,
    restricted_to: list[str] | None = None,
) -> dict[str, Any]:
    """Convenience wrapper: `button(..., action_type="handoff", handoff_target_agent_id=...)`."""
    return button(
        action_id,
        label,
        style=style,
        action_type="handoff",
        handoff_target_agent_id=target_agent_id,
        restricted_to=restricted_to,
    )


def actions(buttons: list[dict[str, Any]]) -> dict[str, Any]:
    """An `actions` block: 1..5 buttons, action_ids must be unique."""
    if not (1 <= len(buttons) <= MAX_BUTTONS_PER_ACTIONS):
        raise ValueError(f"actions needs 1..{MAX_BUTTONS_PER_ACTIONS} elements")
    seen = set()
    for b in buttons:
        aid = b.get("action_id")
        if aid in seen:
            raise ValueError(f"duplicate action_id: {aid!r}")
        seen.add(aid)
    return {"type": "actions", "elements": list(buttons)}


def blocks(*parts: dict[str, Any]) -> list[dict[str, Any]]:
    """Assemble a full `blocks` array, enforcing the MAX_BLOCKS cap."""
    parts_list = list(parts)
    if not parts_list:
        raise ValueError("a card needs at least one block")
    if len(parts_list) > MAX_BLOCKS:
        raise ValueError(f"a card allows at most {MAX_BLOCKS} blocks")
    return parts_list
