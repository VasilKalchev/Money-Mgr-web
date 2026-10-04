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


def request_schema(spec, path, method):
    schema = spec["paths"][path][method]["requestBody"]["content"]["application/json"]["schema"]
    return Draft202012Validator({**schema, "components": spec["components"]})


def test_request_examples_fit_their_schemas(spec):
    for path, method in (("/transactions", "patch"), ("/transactions/{uid}", "patch")):
        media = spec["paths"][path][method]["requestBody"]["content"]["application/json"]
        assert request_schema(spec, path, method).is_valid(media["example"]), (path, method)
    batch = request_schema(spec, "/transactions", "patch")
    assert not batch.is_valid({"changes": [{"note": "no uid"}]})
    assert not batch.is_valid({"changes": [{"uid": "t1", "bogus": 1}]})
    assert not request_schema(spec, "/transactions/{uid}", "patch").is_valid({"uid": "t1", "note": "x"})
    add = request_schema(spec, "/transactions", "post")
    examples = spec["paths"]["/transactions"]["post"]["requestBody"]["content"]["application/json"]["examples"]
    for name, ex in examples.items():
        assert add.is_valid(ex["value"]), name
    assert not add.is_valid({"transactions": []})


def test_reads_match_spec(spec, api):
    check(spec, api.get("status"), "/status", "get", 200)
    check(spec, api.get("accounts"), "/accounts", "get", 200)
    check(spec, api.get("categories"), "/categories", "get", 200)
    check(spec, api.get("categories?show_deleted=show"), "/categories", "get", 200)
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
    r = api.patch("transactions", {"changes": [{"uid": "t1", "note": "y", "expect": {"type": "expense"}}, {"uid": "t2", "note": "z"}]})
    check(spec, r, "/transactions", "patch", 200)
    r = api.post("transactions", {"transactions": [
        {"type": "transfer", "account": "a1", "to_account": "a2", "amount": 9},
        {"type": "expense", "account": "a1", "amount": 1, "category": "c-fun"}]})
    check(spec, r, "/transactions", "post", 201)
    r = api.post("transactions", {"type": "balance_increase", "account": "a1", "amount": 1, "date": "2025-01-01"})
    check(spec, r, "/transactions", "post", 201)
    assert r.get_json()["transaction"]["category_uid"] == "-4"


def test_errors_match_spec(spec, api):
    import users
    check(spec, api.get("transactions?limit=0"), "/transactions", "get", 400)
    check(spec, api.get("transactions/nope"), "/transactions/{uid}", "get", 404)
    check(spec, api.patch("transactions/t1", {"bogus": 1}), "/transactions/{uid}", "patch", 400)
    check(spec, api.patch("transactions/t1", {"note": "x", "expect": {"note": "?"}}), "/transactions/{uid}", "patch", 412)
    r = api.patch("transactions", {"changes": [{"uid": "t1", "note": "x"}, {"uid": "nope", "note": "x"}]})
    check(spec, r, "/transactions", "patch", 400)
    check(spec, api.post("transactions", {"type": "gift"}), "/transactions", "post", 400)
    r = api.post("transactions", {"transactions": [{"type": "gift"}]})
    check(spec, r, "/transactions", "post", 400)
    check(spec, api.get("categories?show_deleted=x"), "/categories", "get", 400)
    check(spec, api.get("status", token="mmw_x_y"), "/status", "get", 401)
    ro = users.create_token("alice", "ro")
    check(spec, api.delete("transactions/t1", token=ro), "/transactions/{uid}", "delete", 403)
