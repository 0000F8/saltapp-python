"""Who is in this chat? Ask Salt's own record of it.

Privacy-sensitive actions -- running a tool on a person's computer, reading
out what they told you in private -- must be decided from Salt's chat record:
fetch `GET /api/v1/chats/:id` (`client.get_chat`) when you need it. NEVER
from a cache, a memory service or a roster you built earlier. Those are fast when they are right and wrong when they are
slow or stale, and a wrong answer here is a privacy error in one direction and
a dead feature in the other: a slow memory service once switched a person's
computer tools off inside their own 1:1.

`is_private_chat` mirrors salt-personal-agent's `brain.chat_is_private`, with
one addition (no observer at all), and fails closed: anything the record
cannot show is NOT private.
"""

from __future__ import annotations

from typing import Any, Optional


def is_private_chat(chat: Optional[dict[str, Any]], speaker_id: Optional[str], my_id: Optional[str]) -> bool:
    """True only when this chat is private to `speaker_id` and `my_id`:
    encrypted, not public, and its members are exactly those two -- a 1:1 or a
    private lane (sidechain, advisor) -- with no observer among them, silent
    or not.

    - `session.encrypted` must not be `False`. An absent flag means an
      ordinary encrypted chat (a server that predates open rooms), as
      everywhere else in this SDK.
    - `session.public` must not be truthy.
    - `session.users` must be a list, every entry a dict with an id. A record
      with no `session`, no member list, or a member without an id is not
      private.
    - Any member flagged `observer` makes it not private
      (`brain.chat_is_private` ignores observers; this does not -- someone
      who can read the chat is in it).
    - `speaker_id` and `my_id` must both be given, and be two different
      accounts.

    Ids compare case-insensitively."""
    if not isinstance(chat, dict) or not speaker_id or not my_id:
        return False
    speaker, me = str(speaker_id).lower(), str(my_id).lower()
    if speaker == me:
        return False
    session = chat.get("session")
    if not isinstance(session, dict):
        return False
    if session.get("public"):
        return False
    if session.get("encrypted") is False:
        return False
    users = session.get("users")
    if not isinstance(users, list):
        return False
    members = set()
    for user in users:
        if not isinstance(user, dict) or user.get("id") in (None, ""):
            return False
        if user.get("observer"):
            return False
        members.add(str(user["id"]).lower())
    return members == {speaker, me}
