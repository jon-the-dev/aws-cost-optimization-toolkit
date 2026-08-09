"""Pytest configuration: make the scripts importable and hand out stubbed AWS clients."""

from __future__ import annotations

import sys
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture
def stubbed():
    """Return a factory yielding a (client, stubber) pair for a given service.

    Credentials are fake and the stubber intercepts every call, so no request ever
    leaves the machine.
    """
    stubbers: list[Stubber] = []

    def _make(service: str, region: str = "us-east-1"):
        session = boto3.Session(
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
            aws_session_token="testing",
            region_name=region,
        )
        client = session.client(service)
        stubber = Stubber(client)
        stubber.activate()
        stubbers.append(stubber)
        return client, stubber

    yield _make

    for stubber in stubbers:
        stubber.deactivate()
