from copy import deepcopy

import pytest


class ScriptedTransport:
    def __init__(self, responses_by_role):
        self._responses = {
            role: list(responses) for role, responses in responses_by_role.items()
        }
        self.calls = []

    def complete(self, **kwargs):
        call = deepcopy(kwargs)
        self.calls.append(call)
        role = kwargs["role"]
        responses = self._responses.get(role, [])
        if not responses:
            raise AssertionError(f"no scripted response remaining for role {role}")
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture
def scripted_transport():
    return ScriptedTransport
