"""docs/openapi.yaml matches the /api/v1/ routes and what they answer."""
import os
import re

import pytest
import yaml
from jsonschema import Draft202012Validator

from test_api import api  # noqa: F401 (fixture)

SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "openapi.yaml")
METHODS = {"get", "post", "patch", "delete", "put"}


@pytest.fixture(scope="module")
def spec():
    with open(SPEC_PATH) as f:
        return yaml.safe_load(f)


def test_spec_is_valid_openapi(spec):
    validator = pytest.importorskip("openapi_spec_validator")
    validator.validate(spec)


def test_spec_covers_every_v1_route(spec):
    from app import app
    routes = {
        (re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", r.rule[len("/api/v1"):]), m.lower())
        for r in app.url_map.iter_rules() if r.rule.startswith("/api/v1/")
        for m in r.methods - {"HEAD", "OPTIONS"}
    }
    documented = {(p, m) for p, ops in spec["paths"].items() for m in ops if m in METHODS}
    assert documented == routes


def check(spec, r, path, method, status):
    """r's status is documented for this operation and its body fits the schema."""
    op = spec["paths"][path][method]
    assert r.status_code == status, r.get_json()
    resp = op["responses"][str(status)]
    if "$ref" in resp:
        resp = spec["components"]["responses"][resp["$ref"].rsplit("/", 1)[1]]
    schema = {**resp["content"]["application/json"]["schema"], "components": spec["components"]}
    errors = list(Draft202012Validator(schema).iter_errors(r.get_json()))
    assert not errors, [f"{list(e.absolute_path)}: {e.message}" for e in errors]


def test_reads_match_spec(spec, api):
    check(spec, api.get("status"), "/status", "get", 200)
    check(spec, api.get("accounts"), "/accounts", "get", 200)
    check(spec, api.get("categories"), "/categories", "get", 200)
    check(spec, api.get("currencies"), "/currencies", "get", 200)
    # every row kind: deleted, mirror, income and expense
    check(spec, api.get("transactions?show_deleted=show&show_mirror=show"), "/transactions", "get", 200)
    check(spec, api.get("transactions/t5"), "/transactions/{uid}", "get", 200)


def test_writes_match_spec(spec, api):
    r = api.post("transactions", {"type": "transfer", "account": "a1", "to_account": "a2", "amount": 9})
    check(spec, r, "/transactions", "post", 201)
    uid = r.get_json()["transaction"]["uid"]
    check(spec, api.patch(f"transactions/{uid}", {"note": "x", "time": "10:00"}), "/transactions/{uid}", "patch", 200)
    check(spec, api.delete(f"transactions/{uid}"), "/transactions/{uid}", "delete", 200)
    r = api.post("transactions", {"type": "balance_increase", "account": "a1", "amount": 1, "date": "2025-01-01"})
    check(spec, r, "/transactions", "post", 201)
    assert r.get_json()["transaction"]["category_uid"] == "-4"


def test_errors_match_spec(spec, api):
    import users
    check(spec, api.get("transactions?limit=0"), "/transactions", "get", 400)
    check(spec, api.get("transactions/nope"), "/transactions/{uid}", "get", 404)
    check(spec, api.patch("transactions/t1", {"bogus": 1}), "/transactions/{uid}", "patch", 400)
    check(spec, api.get("status", token="mmw_x_y"), "/status", "get", 401)
    ro = users.create_token("alice", "ro")
    check(spec, api.delete("transactions/t1", token=ro), "/transactions/{uid}", "delete", 403)
