from __future__ import annotations

import pytest

from saltapp import cards


def test_section_needs_text_or_fields():
    with pytest.raises(ValueError):
        cards.section()
    assert cards.section(text="hi") == {"type": "section", "text": "hi"}
    assert cards.section(fields=[cards.field("Amount", "$5")]) == {
        "type": "section",
        "fields": [{"label": "Amount", "value": "$5"}],
    }


def test_divider():
    assert cards.divider() == {"type": "divider"}


def test_image_requires_http_url():
    with pytest.raises(ValueError):
        cards.image("ftp://example.com/x.png")
    block = cards.image("https://example.com/x.png", alt="a picture")
    assert block == {"type": "image", "url": "https://example.com/x.png", "alt": "a picture"}


def test_button_default():
    b = cards.button("ok", "OK")
    assert b == {"type": "button", "action_id": "ok", "label": "OK"}


def test_pay_button_shape_matches_server_validator():
    # Mirrors salt-api's card.rb validate_actions "pay" branch: amount is a
    # human-decimal STRING, never base units.
    b = cards.pay_button("pay1", "Pay", amount="1.50", currency="USDC")
    assert b["action_type"] == "pay"
    assert b["pay"] == {"amount": "1.50", "currency": "USDC"}


def test_pay_button_requires_amount_and_currency():
    with pytest.raises(ValueError):
        cards.button("pay1", "Pay", action_type="pay", pay={"amount": "1.50"})


def test_handoff_button_shape():
    b = cards.handoff_button("h1", "Hand off", target_agent_id="agent-123")
    assert b == {
        "type": "button",
        "action_id": "h1",
        "label": "Hand off",
        "action_type": "handoff",
        "handoff_target_agent_id": "agent-123",
    }


def test_button_rejects_bad_style():
    with pytest.raises(ValueError):
        cards.button("a", "A", style="cute")


def test_button_restricted_to():
    b = cards.button("a", "A", restricted_to=["u1", "u2"])
    assert b["restricted_to"] == ["u1", "u2"]


def test_actions_needs_one_to_five_buttons():
    with pytest.raises(ValueError):
        cards.actions([])
    with pytest.raises(ValueError):
        cards.actions([cards.button(f"b{i}", "B") for i in range(6)])
    block = cards.actions([cards.button("b1", "B1"), cards.button("b2", "B2")])
    assert block["type"] == "actions"
    assert len(block["elements"]) == 2


def test_actions_rejects_duplicate_action_ids():
    with pytest.raises(ValueError):
        cards.actions([cards.button("dup", "One"), cards.button("dup", "Two")])


def test_blocks_enforces_max_and_nonempty():
    with pytest.raises(ValueError):
        cards.blocks()
    with pytest.raises(ValueError):
        cards.blocks(*[cards.divider() for _ in range(cards.MAX_BLOCKS + 1)])
    result = cards.blocks(cards.section(text="hi"), cards.divider())
    assert result == [{"type": "section", "text": "hi"}, {"type": "divider"}]
