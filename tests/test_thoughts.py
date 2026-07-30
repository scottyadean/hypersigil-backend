""" Handler tests backed by an in-memory mongo """
import json

import mongomock
import pytest

from src import thoughs, utils


@pytest.fixture(autouse=True)
def collection(monkeypatch):
    """ point the handlers at an in-memory collection """
    client = mongomock.MongoClient()
    thoughts = client["hypersigil"]["thoughts"]
    monkeypatch.setattr(utils, "get_thoughts_collection", lambda: thoughts)
    monkeypatch.setattr(thoughs, "get_thoughts_collection", lambda: thoughts)
    return thoughts


def body(result):
    return json.loads(result["body"])


def post(name, thought):
    return thoughs.create_thought({"body": json.dumps({"name": name, "thought": thought})}, None)


def test_create_returns_the_stored_thought(collection):
    result = post("scott", "manifesting a working deploy")

    assert result["statusCode"] == 201
    payload = body(result)
    assert payload["name"] == "scott"
    assert payload["thought"] == "manifesting a working deploy"
    assert payload["id"]
    assert payload["created_at"]
    assert collection.count_documents({}) == 1


def test_create_trims_whitespace():
    payload = body(post("  scott  ", "  a thought  "))
    assert payload["name"] == "scott"
    assert payload["thought"] == "a thought"


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"body": ""},
        {"body": "not json"},
        {"body": json.dumps(["a", "list"])},
        {"body": json.dumps({"thought": "no name"})},
        {"body": json.dumps({"name": "scott"})},
        {"body": json.dumps({"name": "   ", "thought": "blank name"})},
        {"body": json.dumps({"name": 7, "thought": "numeric name"})},
        {"body": json.dumps({"name": "x" * (utils.NAME_MAX + 1), "thought": "long name"})},
        {"body": json.dumps({"name": "scott", "thought": "x" * (utils.THOUGHT_MAX + 1)})},
    ],
)
def test_create_rejects_bad_input(event):
    result = thoughs.create_thought(event, None)
    assert result["statusCode"] == 400
    assert "error" in body(result)


def test_list_is_newest_first():
    post("a", "first")
    post("b", "second")
    post("c", "third")

    payload = body(thoughs.list_thoughts({}, None))
    assert payload["count"] == 3
    assert [t["name"] for t in payload["thoughts"]] == ["c", "b", "a"]


def test_list_handles_empty_collection():
    result = thoughs.list_thoughts({"queryStringParameters": None}, None)
    assert result["statusCode"] == 200
    assert body(result) == {"thoughts": [], "count": 0}


def test_list_respects_limit():
    for index in range(5):
        post(f"user{index}", "thought")

    payload = body(thoughs.list_thoughts({"queryStringParameters": {"limit": "2"}}, None))
    assert payload["count"] == 2


def test_list_caps_limit_at_the_maximum(collection):
    for index in range(3):
        post(f"user{index}", "thought")

    result = thoughs.list_thoughts(
        {"queryStringParameters": {"limit": str(utils.MAX_LIMIT + 5000)}}, None
    )
    assert body(result)["count"] == 3

    # The clamp itself, not just the fact that a big number is accepted.
    seen = {}
    original = collection.find

    def spy(*args, **kwargs):
        cursor = original(*args, **kwargs)
        real_limit = cursor.limit

        def capture(value):
            seen["limit"] = value
            return real_limit(value)

        cursor.limit = capture
        return cursor

    collection.find = spy
    try:
        thoughs.list_thoughts({"queryStringParameters": {"limit": "999999"}}, None)
    finally:
        collection.find = original

    assert seen["limit"] == utils.MAX_LIMIT


@pytest.mark.parametrize("limit", ["abc", "0", "-1"])
def test_list_rejects_a_bad_limit(limit):
    result = thoughs.list_thoughts({"queryStringParameters": {"limit": limit}}, None)
    assert result["statusCode"] == 400


def test_get_returns_a_single_thought():
    created = body(post("scott", "a specific thought"))

    result = thoughs.get_thought({"pathParameters": {"thought_id": created["id"]}}, None)
    assert result["statusCode"] == 200
    assert body(result) == created


def test_get_404s_for_an_unknown_id():
    result = thoughs.get_thought({"pathParameters": {"thought_id": "507f1f77bcf86cd799439011"}}, None)
    assert result["statusCode"] == 404


@pytest.mark.parametrize("thought_id", ["not-an-id", "", None])
def test_get_400s_for_a_malformed_id(thought_id):
    result = thoughs.get_thought({"pathParameters": {"thought_id": thought_id}}, None)
    assert result["statusCode"] == 400


def test_get_400s_when_the_path_param_is_missing():
    assert thoughs.get_thought({}, None)["statusCode"] == 400


def flag(thought_id):
    return thoughs.flag_thought({"pathParameters": {"thought_id": thought_id}}, None)


def test_new_thoughts_start_with_zero_flags():
    assert body(post("scott", "unflagged"))["flags"] == 0


def test_flag_increments_the_count():
    created = body(post("scott", "flag me"))

    payload = body(flag(created["id"]))
    assert payload["flags"] == 1
    assert payload["hidden"] is False
    assert payload["threshold"] == utils.FLAG_THRESHOLD

    assert body(flag(created["id"]))["flags"] == 2


def test_flag_count_is_returned_by_list_and_get():
    created = body(post("scott", "flag me"))
    flag(created["id"])
    flag(created["id"])

    assert body(thoughs.list_thoughts({}, None))["thoughts"][0]["flags"] == 2
    assert body(thoughs.get_thought({"pathParameters": {"thought_id": created["id"]}}, None))["flags"] == 2


def test_thought_is_hidden_at_the_threshold():
    created = body(post("scott", "about to vanish"))
    post("ada", "stays visible")

    for count in range(1, utils.FLAG_THRESHOLD):
        payload = body(flag(created["id"]))
        assert payload["hidden"] is False, f"hidden too early at {count} flags"

    # The flag that reaches the threshold is the one that hides it.
    final = body(flag(created["id"]))
    assert final["flags"] == utils.FLAG_THRESHOLD
    assert final["hidden"] is True

    listed = body(thoughs.list_thoughts({}, None))
    assert listed["count"] == 1
    assert [t["name"] for t in listed["thoughts"]] == ["ada"]


def test_hidden_thought_is_not_reachable_by_direct_get():
    created = body(post("scott", "about to vanish"))
    for _ in range(utils.FLAG_THRESHOLD):
        flag(created["id"])

    result = thoughs.get_thought({"pathParameters": {"thought_id": created["id"]}}, None)
    assert result["statusCode"] == 404


def test_thoughts_predating_the_flags_field_are_still_listed(collection):
    # Documents written before flagging existed have no flags key, and a bare
    # $lt filter would silently drop every one of them.
    collection.insert_one({"name": "legacy", "thought": "no flags key", "created_at": utils.utc_now()})

    listed = body(thoughs.list_thoughts({}, None))
    assert listed["count"] == 1
    assert listed["thoughts"][0]["flags"] == 0


def test_flagging_a_legacy_thought_starts_the_count_at_one(collection):
    collection.insert_one({"name": "legacy", "thought": "no flags key", "created_at": utils.utc_now()})
    thought_id = str(collection.find_one({})["_id"])

    assert body(flag(thought_id))["flags"] == 1


def test_flag_404s_for_an_unknown_id():
    assert flag("507f1f77bcf86cd799439011")["statusCode"] == 404


@pytest.mark.parametrize("thought_id", ["not-an-id", "", None])
def test_flag_400s_for_a_malformed_id(thought_id):
    assert flag(thought_id)["statusCode"] == 400


def test_flag_400s_when_the_path_param_is_missing():
    assert thoughs.flag_thought({}, None)["statusCode"] == 400


PASSCODE = "clever_ab12cd34ef56_otter"


def post_with_passcode(name, thought, passcode=PASSCODE):
    return thoughs.create_thought(
        {"body": json.dumps({"name": name, "thought": thought, "passcode": passcode})}, None
    )


def edit(thought_id, payload):
    return thoughs.edit_thought(
        {"pathParameters": {"thought_id": thought_id}, "body": json.dumps(payload)}, None
    )


def test_no_endpoint_ever_returns_the_passcode_or_its_hash(collection):
    """ Every response body, checked as raw text, for every route. """
    created = body(post_with_passcode("scott", "secret"))
    thought_id = created["id"]
    stored_hash = collection.find_one({})["passcode_hash"]

    raw_responses = {
        "create": thoughs.create_thought(
            {"body": json.dumps({"name": "b", "thought": "t", "passcode": PASSCODE})}, None
        )["body"],
        "list": thoughs.list_thoughts({}, None)["body"],
        "get": thoughs.get_thought({"pathParameters": {"thought_id": thought_id}}, None)["body"],
        "edit": edit(thought_id, {"passcode": PASSCODE, "thought": "edited"})["body"],
        "edit_rejected": edit(thought_id, {"passcode": "wrong_passcode", "thought": "x"})["body"],
        "flag": flag(thought_id)["body"],
    }

    for route, raw in raw_responses.items():
        assert PASSCODE not in raw, f"{route} leaked the plaintext passcode"
        assert stored_hash not in raw, f"{route} leaked the stored hash"
        assert "passcode" not in raw.lower() or route == "edit_rejected", (
            f"{route} mentions passcode: {raw}"
        )
        assert "pbkdf2" not in raw, f"{route} leaked hash material"
        assert "salt" not in raw.lower(), f"{route} leaked hash material"

    # The rejection message may say the word "passcode" but must carry no value.
    assert raw_responses["edit_rejected"] == json.dumps({"error": "passcode does not match"})

    # Stored hashed, never in plaintext.
    assert stored_hash.startswith("pbkdf2_sha256$")
    assert PASSCODE not in stored_hash
    assert created["editable"] is True


def test_thought_without_a_passcode_is_not_editable():
    created = body(post("scott", "no passcode"))
    assert created["editable"] is False

    result = edit(created["id"], {"passcode": PASSCODE, "thought": "hijacked"})
    assert result["statusCode"] == 403


def test_edit_with_the_right_passcode_updates_the_thought():
    created = body(post_with_passcode("scott", "first draft"))

    payload = body(edit(created["id"], {"passcode": PASSCODE, "thought": "second draft"}))
    assert payload["thought"] == "second draft"
    assert payload["id"] == created["id"]
    assert payload["created_at"] == created["created_at"]
    assert payload["updated_at"] is not None

    fetched = body(thoughs.get_thought({"pathParameters": {"thought_id": created["id"]}}, None))
    assert fetched["thought"] == "second draft"


def test_edit_can_change_the_name_too():
    created = body(post_with_passcode("scott", "text"))
    payload = body(edit(created["id"], {"passcode": PASSCODE, "thought": "text", "name": "ada"}))
    assert payload["name"] == "ada"


def test_edit_leaves_the_name_alone_when_not_supplied():
    created = body(post_with_passcode("scott", "text"))
    payload = body(edit(created["id"], {"passcode": PASSCODE, "thought": "new text"}))
    assert payload["name"] == "scott"


@pytest.mark.parametrize(
    "wrong",
    ["wrong_passcode_here", PASSCODE.upper(), PASSCODE + "x", PASSCODE[:-1]],
)
def test_edit_rejects_a_wrong_passcode(wrong):
    created = body(post_with_passcode("scott", "original"))

    result = edit(created["id"], {"passcode": wrong, "thought": "hijacked"})
    assert result["statusCode"] == 403

    fetched = body(thoughs.get_thought({"pathParameters": {"thought_id": created["id"]}}, None))
    assert fetched["thought"] == "original"


def test_edit_does_not_preserve_flags_or_let_a_hidden_thought_return():
    created = body(post_with_passcode("scott", "controversial"))
    for _ in range(utils.FLAG_THRESHOLD):
        flag(created["id"])

    result = edit(created["id"], {"passcode": PASSCODE, "thought": "rewritten"})
    assert result["statusCode"] == 404
    assert body(thoughs.list_thoughts({}, None))["count"] == 0


def test_edit_keeps_the_flag_count():
    created = body(post_with_passcode("scott", "text"))
    flag(created["id"])
    flag(created["id"])

    payload = body(edit(created["id"], {"passcode": PASSCODE, "thought": "edited"}))
    assert payload["flags"] == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"thought": "no passcode"},
        {"passcode": PASSCODE},
        {"passcode": "short", "thought": "too short a passcode"},
        {"passcode": PASSCODE, "thought": ""},
        {"passcode": PASSCODE, "thought": "x" * (utils.THOUGHT_MAX + 1)},
        {"passcode": 12345, "thought": "numeric passcode"},
    ],
)
def test_edit_rejects_bad_input(payload):
    created = body(post_with_passcode("scott", "original"))
    assert edit(created["id"], payload)["statusCode"] == 400


def test_edit_404s_for_an_unknown_id():
    result = edit("507f1f77bcf86cd799439011", {"passcode": PASSCODE, "thought": "x"})
    assert result["statusCode"] == 404


def test_edit_400s_for_a_malformed_id():
    assert edit("not-an-id", {"passcode": PASSCODE, "thought": "x"})["statusCode"] == 400


def test_create_rejects_a_too_short_passcode():
    result = thoughs.create_thought(
        {"body": json.dumps({"name": "scott", "thought": "x", "passcode": "abc"})}, None
    )
    assert result["statusCode"] == 400


def test_the_same_passcode_hashes_differently_each_time(collection):
    post_with_passcode("a", "one")
    post_with_passcode("b", "two")

    hashes = [d["passcode_hash"] for d in collection.find({})]
    assert hashes[0] != hashes[1], "salt is not being applied"
    assert all(utils.verify_passcode(PASSCODE, h) for h in hashes)


@pytest.mark.parametrize("stored", [None, "", "garbage", "pbkdf2_sha256$notanint$aa$bb", "md5$1$aa$bb"])
def test_verify_passcode_rejects_malformed_stored_hashes(stored):
    assert utils.verify_passcode(PASSCODE, stored) is False


def test_responses_carry_cors_headers():
    for result in (
        post("scott", "cors"),
        thoughs.list_thoughts({}, None),
        thoughs.get_thought({"pathParameters": {"thought_id": "bad"}}, None),
    ):
        assert result["headers"]["Access-Control-Allow-Origin"] == "*"
        assert result["headers"]["Content-Type"] == "application/json"
