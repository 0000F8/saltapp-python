from __future__ import annotations

import pytest

from saltapp import crypto


@pytest.fixture(scope="session")
def keypair_a():
    return crypto.generate_keypair("passphrase-a")


@pytest.fixture(scope="session")
def keypair_b():
    return crypto.generate_keypair("passphrase-b")
