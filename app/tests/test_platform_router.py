from app.platform.router import _default_workspace_id


def test_default_workspace_id_normalizes_token_claims():
    assert _default_workspace_id({"default_workspace_id": 42}) == 42
    assert _default_workspace_id({"default_workspace_id": "42"}) == 42
    assert _default_workspace_id({"default_workspace_id": None}) is None
    assert _default_workspace_id({"default_workspace_id": "not-an-int"}) is None
