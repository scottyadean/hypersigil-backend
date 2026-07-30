""" Thought handlers """
import logging

from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from src.utils import (
    DEFAULT_LIMIT,
    FLAG_THRESHOLD,
    MAX_LIMIT,
    NAME_MAX,
    THOUGHT_MAX,
    clean_passcode,
    clean_text,
    error,
    get_thoughts_collection,
    hash_passcode,
    parse_body,
    response,
    serialize_thought,
    to_object_id,
    utc_now,
    verify_passcode,
    visible_filter,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def create_thought(event, _context):
    """ create a new thought """
    data, parse_error = parse_body(event)
    if parse_error:
        return error(400, parse_error)

    name, name_error = clean_text(data.get("name"), NAME_MAX)
    if name_error:
        return error(400, f"name: {name_error}")

    thought, thought_error = clean_text(data.get("thought"), THOUGHT_MAX)
    if thought_error:
        return error(400, f"thought: {thought_error}")

    document = {"name": name, "thought": thought, "created_at": utc_now(), "flags": 0}

    # Optional: a thought published without one simply cannot be edited later.
    if data.get("passcode") is not None:
        passcode, passcode_error = clean_passcode(data.get("passcode"))
        if passcode_error:
            return error(400, f"passcode: {passcode_error}")
        document["passcode_hash"] = hash_passcode(passcode)

    try:
        result = get_thoughts_collection().insert_one(document)
    except PyMongoError:
        logger.exception("failed to insert thought")
        return error(503, "could not reach the database")

    document["_id"] = result.inserted_id
    return response(201, serialize_thought(document))


def list_thoughts(event, _context):
    """ list all thoughts """
    params = event.get("queryStringParameters") or {}

    try:
        limit = int(params.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        return error(400, "limit must be an integer")
    if limit < 1:
        return error(400, "limit must be at least 1")
    limit = min(limit, MAX_LIMIT)

    try:
        # _id breaks ties: several thoughts can land in the same millisecond,
        # and sorting on created_at alone leaves their order undefined.
        cursor = (
            get_thoughts_collection()
            .find(visible_filter())
            .sort([("created_at", -1), ("_id", -1)])
            .limit(limit)
        )
        thoughts = [serialize_thought(document) for document in cursor]
    except PyMongoError:
        logger.exception("failed to list thoughts")
        return error(503, "could not reach the database")

    return response(200, {"thoughts": thoughts, "count": len(thoughts)})


def get_thought(event, _context):
    """ retrieve a specific thought """
    path_params = event.get("pathParameters") or {}
    thought_id = to_object_id(path_params.get("thought_id"))
    if thought_id is None:
        return error(400, "thought_id is not a valid id")

    try:
        # A thought hidden by flags 404s here too, so a direct link cannot be
        # used to read something the feed has already taken down.
        document = get_thoughts_collection().find_one({"_id": thought_id, **visible_filter()})
    except PyMongoError:
        logger.exception("failed to fetch thought")
        return error(503, "could not reach the database")

    if document is None:
        return error(404, "thought not found")

    return response(200, serialize_thought(document))


def edit_thought(event, _context):
    """ update a thought for a caller who knows its passcode """
    path_params = event.get("pathParameters") or {}
    thought_id = to_object_id(path_params.get("thought_id"))
    if thought_id is None:
        return error(400, "thought_id is not a valid id")

    data, parse_error = parse_body(event)
    if parse_error:
        return error(400, parse_error)

    passcode, passcode_error = clean_passcode(data.get("passcode"))
    if passcode_error:
        return error(400, f"passcode: {passcode_error}")

    thought, thought_error = clean_text(data.get("thought"), THOUGHT_MAX)
    if thought_error:
        return error(400, f"thought: {thought_error}")

    updates = {"thought": thought, "updated_at": utc_now()}
    if "name" in data:
        name, name_error = clean_text(data.get("name"), NAME_MAX)
        if name_error:
            return error(400, f"name: {name_error}")
        updates["name"] = name

    try:
        # visible_filter keeps a flagged-down thought from being edited back
        # into circulation.
        document = get_thoughts_collection().find_one({"_id": thought_id, **visible_filter()})
    except PyMongoError:
        logger.exception("failed to load thought for edit")
        return error(503, "could not reach the database")

    if document is None:
        return error(404, "thought not found")

    if not document.get("passcode_hash"):
        return error(403, "this thought was published without a passcode and cannot be edited")

    if not verify_passcode(passcode, document["passcode_hash"]):
        return error(403, "passcode does not match")

    try:
        updated = get_thoughts_collection().find_one_and_update(
            {"_id": thought_id},
            {"$set": updates},
            return_document=ReturnDocument.AFTER,
        )
    except PyMongoError:
        logger.exception("failed to update thought")
        return error(503, "could not reach the database")

    return response(200, serialize_thought(updated))


def flag_thought(event, _context):
    """ flag a thought, hiding it once it reaches the flag threshold """
    path_params = event.get("pathParameters") or {}
    thought_id = to_object_id(path_params.get("thought_id"))
    if thought_id is None:
        return error(400, "thought_id is not a valid id")

    try:
        # A single atomic increment - two people flagging at the same moment
        # both count, which a read-modify-write would lose.
        document = get_thoughts_collection().find_one_and_update(
            {"_id": thought_id},
            {"$inc": {"flags": 1}},
            return_document=ReturnDocument.AFTER,
        )
    except PyMongoError:
        logger.exception("failed to flag thought")
        return error(503, "could not reach the database")

    if document is None:
        return error(404, "thought not found")

    flags = document.get("flags") or 0
    return response(
        200,
        {
            "id": str(document["_id"]),
            "flags": flags,
            "hidden": flags >= FLAG_THRESHOLD,
            "threshold": FLAG_THRESHOLD,
        },
    )
