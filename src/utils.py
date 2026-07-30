""" Shared helpers for the hypersigil handlers """
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import MongoClient
from pymongo.server_api import ServerApi

URI = os.getenv("DB_URL")
DB_NAME = os.getenv("DB_NAME", "hypersigil")
COLLECTION = "thoughts"
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")

NAME_MAX = 255
THOUGHT_MAX = 1500
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
FLAG_THRESHOLD = 20
PASSCODE_MIN = 6
PASSCODE_MAX = 200
PBKDF2_ITERATIONS = 200_000

# Cached across warm lambda invocations. Building a MongoClient per request
# opens a new connection pool every time and exhausts the Atlas connection
# limit under any real traffic.
_client = None


def get_client():
    """ get a cached mongo client """
    global _client
    if _client is None:
        if not URI:
            raise RuntimeError("DB_URL is not set")
        _client = MongoClient(
            URI,
            server_api=ServerApi("1"),
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
        )
    return _client


def get_db():
    """ get the hypersigil database """
    return get_client()[DB_NAME]


def get_thoughts_collection():
    """ get the thoughts collection """
    return get_db()[COLLECTION]


def response(status_code, body):
    """ build an api gateway proxy response """
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
        },
        "body": json.dumps(body, default=str),
    }


def error(status_code, message):
    """ build an error response """
    return response(status_code, {"error": message})


def parse_body(event):
    """ parse a json request body, returns (data, error_message) """
    raw = event.get("body")
    if not raw:
        return None, "request body is required"
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None, "request body must be valid json"
    if not isinstance(data, dict):
        return None, "request body must be a json object"
    return data, None


def clean_text(value, max_length):
    """ coerce a submitted field to a trimmed string, returns (value, error_message) """
    if value is None:
        return None, "field is required"
    if not isinstance(value, str):
        return None, "field must be a string"
    value = value.strip()
    if not value:
        return None, "field cannot be empty"
    if len(value) > max_length:
        return None, f"field cannot be longer than {max_length} characters"
    return value, None


def to_object_id(value):
    """ convert a path param to an ObjectId, returns None when malformed """
    # ObjectId(None) mints a brand new random id rather than raising, so a
    # missing path param has to be rejected before it reaches the constructor.
    if not value:
        return None
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def format_timestamp(value):
    """ render a stored datetime as an unambiguous utc iso string """
    if not isinstance(value, datetime):
        return value
    # Mongo stores datetimes as utc but hands them back naive. Left alone, the
    # browser's Date parser reads a naive iso string as local time and shifts
    # every thought by the viewer's utc offset.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def visible_filter():
    """ mongo filter matching thoughts that have not been flagged into hiding """
    # Thoughts written before flagging existed have no flags field at all, and
    # a bare $lt will not match a missing field.
    return {
        "$or": [
            {"flags": {"$lt": FLAG_THRESHOLD}},
            {"flags": {"$exists": False}},
        ]
    }


def clean_passcode(value):
    """ validate a submitted passcode, returns (value, error_message) """
    value, message = clean_text(value, PASSCODE_MAX)
    if message:
        return None, message
    if len(value) < PASSCODE_MIN:
        return None, f"field must be at least {PASSCODE_MIN} characters"
    return value, None


def hash_passcode(passcode):
    """ derive a salted storable hash for a passcode """
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_passcode(passcode, stored):
    """ check a passcode against a stored hash """
    if not isinstance(stored, str) or not isinstance(passcode, str):
        return False
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        iterations = int(iterations)
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), salt, iterations)
    # Constant time, so a caller cannot time their way to a passcode byte by byte.
    return hmac.compare_digest(candidate, expected)


def serialize_thought(document):
    """ convert a mongo document into the api representation """
    # passcode_hash is deliberately absent - it must never leave the backend.
    return {
        "id": str(document["_id"]),
        "name": document.get("name"),
        "thought": document.get("thought"),
        "created_at": format_timestamp(document.get("created_at")),
        "updated_at": format_timestamp(document.get("updated_at")),
        "flags": document.get("flags") or 0,
        "editable": bool(document.get("passcode_hash")),
    }


def utc_now():
    """ current time as a timezone aware utc datetime """
    # Truncated to milliseconds to match what BSON can actually store, so the
    # timestamp returned by a create matches the one a later read returns.
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)
