# is_private_chat mirrors salt-personal-agent's brain.chat_is_private, plus
# "no observer at all", and fails closed on anything the chat record cannot
# show. Mirrors salt-agent-sdk's tests/private-chat.test.js.
from __future__ import annotations

import pytest

from saltapp import is_private_chat

SPEAKER = "00000000-0000-0000-0000-0000000000b2"
ME = "00000000-0000-0000-0000-0000000000a1"


def record(session):
    return {"id": "chat-1", "session": session}


def one_to_one():
    return record({"encrypted": True, "public": False, "users": [{"id": SPEAKER}, {"id": ME, "observer": False}]})


def test_an_encrypted_non_public_one_to_one_of_exactly_the_speaker_and_me_is_private():
    assert is_private_chat(one_to_one(), SPEAKER, ME) is True


def test_a_private_lane_is_private_too_like_brain_chat_is_private():
    lane = record({"encrypted": True, "users": [{"id": SPEAKER}, {"id": ME}], "coaching_for_chat_id": "room-1"})
    assert is_private_chat(lane, SPEAKER, ME) is True


def test_an_absent_encrypted_flag_means_encrypted_an_explicit_false_does_not():
    assert is_private_chat(record({"users": [{"id": SPEAKER}, {"id": ME}]}), SPEAKER, ME) is True
    assert is_private_chat(record({"encrypted": False, "users": [{"id": SPEAKER}, {"id": ME}]}), SPEAKER, ME) is False


def test_a_public_chat_is_never_private():
    assert is_private_chat(record({"encrypted": True, "public": True, "users": [{"id": SPEAKER}, {"id": ME}]}), SPEAKER, ME) is False


def test_a_third_member_makes_it_not_private():
    group = record({"encrypted": True, "users": [{"id": SPEAKER}, {"id": ME}, {"id": "someone-else"}]})
    assert is_private_chat(group, SPEAKER, ME) is False


def test_any_observer_silent_or_not_makes_it_not_private():
    watched = record({"encrypted": True, "users": [{"id": SPEAKER}, {"id": ME}, {"id": "owner-1", "observer": True}]})
    assert is_private_chat(watched, SPEAKER, ME) is False
    me_observing = record({"encrypted": True, "users": [{"id": SPEAKER}, {"id": ME, "observer": True}]})
    assert is_private_chat(me_observing, SPEAKER, ME) is False


def test_someone_other_than_the_speaker_is_not_private_to_the_speaker():
    assert is_private_chat(one_to_one(), "someone-else", ME) is False
    other = record({"encrypted": True, "users": [{"id": "someone-else"}, {"id": ME}]})
    assert is_private_chat(other, SPEAKER, ME) is False


def test_a_chat_with_only_me_or_only_the_speaker_is_not_a_conversation_between_the_two():
    assert is_private_chat(record({"encrypted": True, "users": [{"id": ME}]}), SPEAKER, ME) is False
    assert is_private_chat(record({"encrypted": True, "users": [{"id": SPEAKER}]}), SPEAKER, ME) is False
    assert is_private_chat(one_to_one(), ME, ME) is False


@pytest.mark.parametrize(
    "chat,speaker,me",
    [
        (None, SPEAKER, ME),
        ({}, SPEAKER, ME),
        ({"session": "nope"}, SPEAKER, ME),
        ({"session": {"encrypted": True}}, SPEAKER, ME),
        ({"session": {"encrypted": True, "users": "nope"}}, SPEAKER, ME),
        ({"session": {"encrypted": True, "users": [{"id": SPEAKER}, {"id": ME}, {}]}}, SPEAKER, ME),
        ({"session": {"encrypted": True, "users": [{"id": SPEAKER}, {"id": ME}, None]}}, SPEAKER, ME),
        (one_to_one(), None, ME),
        (one_to_one(), SPEAKER, None),
        (one_to_one(), "", ME),
    ],
)
def test_any_missing_field_fails_closed(chat, speaker, me):
    assert is_private_chat(chat, speaker, me) is False


def test_ids_compare_case_insensitively():
    assert is_private_chat(one_to_one(), SPEAKER.upper(), ME.upper()) is True
