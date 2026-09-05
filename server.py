import os
import json
from functools import wraps
from datetime import datetime, timezone
from dotenv import main
from urllib.parse import quote_plus
from pymongo import MongoClient, DESCENDING
from flask import Flask, request, g
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash
from bson import json_util
from flask_cors import CORS
from itsdangerous import URLSafeTimedSerializer, SignatureExpired
from utils import sort_work_items

main.load_dotenv()
db_user = os.getenv('DBUSER')
db_pass = os.getenv('DBPASS')

# ---------------------------------------------------------------------------
# Session configuration
#
# ADMIN_TOKEN and require_token are gone. A door is only as strong as the
# weakest credential it accepts, and a non-expiring shared secret sitting
# beside the session token would have made the real security "one string in a
# .env", with no identity for the backup record and no expiry.
#
# What survived from require_token is its shape: fail closed when unconfigured,
# never hand-compare a secret, apply the guard to every write route.
# ---------------------------------------------------------------------------

SESSION_SECRET = (os.getenv('ADMIN_SESSION_SECRET') or '').strip()

# A short signing key is an offline oracle, not an inconvenience: a token
# carries its own payload AND signature, so anyone holding one can brute-force
# the key at full speed without touching this server.
MIN_SESSION_SECRET_LENGTH = 32

# itsdangerous's "salt" is namespacing, not a cryptographic salt. Bumping the
# version here invalidates every outstanding token without rotating the key.
SESSION_SALT = 'resume-admin-session-v1'

SESSION_TTL_SECONDS = 8 * 60 * 60

MAX_USERNAME_LENGTH = 128
MAX_PASSWORD_LENGTH = 1024

# Refuse an oversized body before Flask parses JSON and before scrypt runs.
MAX_CONTENT_LENGTH = 256 * 1024

# Generations kept per resume. A count, not a capped collection (which evicts
# by bytes, so retention depends on how long the description happens to be)
# and not a TTL index (age-based retention is backwards here — go quiet for two
# years and a 365-day TTL deletes every backup right before you need one).
BACKUP_KEEP = 50

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

CORS(app, origins=["http://localhost:3000", "http://127.0.0.1:3000"],
     supports_credentials=True)

# quote_plus, because a password containing @ / : or ? would otherwise be
# parsed as URI structure rather than as a credential.
CONNECTION_STRING = (
    f"mongodb+srv://{quote_plus(db_user or '')}:{quote_plus(db_pass or '')}"
    "@personalsite.qbhpviu.mongodb.net/?retryWrites=true&w=majority&appName=PersonalSite"
)

_client = None


def get_client():
    """Build the MongoClient once, lazily, on first use.

    Constructed at import instead, this kills the container rather than a
    request. A mongodb+srv:// URI resolves DNS inside the MongoClient
    constructor, so a DNS failure raises during app import — and a gunicorn
    worker that dies while loading the app exits 3, which the arbiter treats
    as fatal for the whole master. A transient blip at NAS boot, when Docker
    can start containers before WAN DNS is up, would take the API down and
    keep it down.

    Built lazily, the same failure surfaces inside a request handler, where
    get_resume's try/except turns it into a 500 and the next request recovers
    on its own.

    connect=False additionally defers the SRV lookup out of the constructor
    (measured: an unresolvable SRV URI raises in 0.12s with the default, and
    not at all with connect=False). Belt and braces.

    One client, not two: get_database() used to be called twice at module
    scope, giving two topologies, two connection pools and two SRV monitors
    for one small document.
    """
    global _client
    if _client is None:
        _client = MongoClient(CONNECTION_STRING, connect=False)
    return _client


def resumes():
    return get_client()['test']['resumes']


def admins():
    """One document per admin: {username, password_hash}. The field is
    password_hash, never password — the collection this replaces stored
    plaintext."""
    return get_client()['test']['admins']


def backups():
    """Prior versions of the resume, one document per applied write."""
    return get_client()['test']['resume_backups']


def jsonify(text):
    return json.loads(json_util.dumps(text))


# ===========================================================================
# Errors — every failure from this app is JSON, including Flask's own
# ===========================================================================

NOT_CONFIGURED = ({"error": "Server is not configured for admin sessions",
                   "code": "not_configured"}, 503)
INVALID_CREDENTIALS = ({"error": "Invalid username or password",
                        "code": "invalid_credentials"}, 401)
SERVER_ERROR = ({"error": "Database connection failed",
                 "code": "server_error"}, 500)


def unauthorized():
    return ({"error": "Unauthorized", "code": "unauthorized"}, 401,
            {"WWW-Authenticate": 'Bearer error="invalid_token"'})


@app.errorhandler(HTTPException)
def http_error_as_json(e):
    """Without this, a 413 or a 405 returns Flask's HTML page and the frontend
    tries to parse it as JSON."""
    return {"error": e.description, "code": e.name.lower().replace(' ', '_')}, e.code


# ===========================================================================
# Sessions
# ===========================================================================

def session_secret_ok():
    return len(SESSION_SECRET) >= MIN_SESSION_SECRET_LENGTH


def get_serializer():
    return URLSafeTimedSerializer(SESSION_SECRET, salt=SESSION_SALT)


def require_session(handler):
    """Gate a route behind a valid session token.

    Fails closed: with no signing secret configured the route returns 503
    rather than running, so a missing env var can never silently open a write
    endpoint.
    """
    @wraps(handler)
    def wrapper(*args, **kwargs):
        if not session_secret_ok():
            print("ADMIN_SESSION_SECRET is missing or too short - refusing write")
            return NOT_CONFIGURED

        header = request.headers.get('Authorization', '')
        if not header.startswith('Bearer '):
            return unauthorized()

        try:
            payload = get_serializer().loads(
                header[len('Bearer '):].strip(), max_age=SESSION_TTL_SECONDS)
        except SignatureExpired:
            return ({"error": "Session expired", "code": "session_expired"}, 401,
                    {"WWW-Authenticate": 'Bearer error="invalid_token"'})
        except Exception:
            # BadSignature, a malformed token, bad base64 - all unauthorized.
            return unauthorized()

        username = payload.get('u') if isinstance(payload, dict) else None
        if not isinstance(username, str) or not username:
            return unauthorized()

        g.admin_username = username
        return handler(*args, **kwargs)
    return wrapper


@app.route('/session', methods=['POST'])
def create_session():
    print("POST /session endpoint hit")
    if not session_secret_ok():
        return NOT_CONFIGURED

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return {"error": "Expected a JSON object body", "code": "bad_request"}, 400

    username = body.get('username')
    password = body.get('password')
    # Length-checked before scrypt sees it: a megabyte password would
    # otherwise buy an attacker a megabyte of key derivation.
    if (not isinstance(username, str) or not isinstance(password, str)
            or not username or not password
            or len(username) > MAX_USERNAME_LENGTH
            or len(password) > MAX_PASSWORD_LENGTH):
        return {"error": "username and password are required strings",
                "code": "bad_request"}, 400

    try:
        admin = admins().find_one({'username': username})
    except Exception as e:
        print(f"Database error during login: {str(e)}")
        return SERVER_ERROR

    stored = (admin or {}).get('password_hash')
    if not admin or not isinstance(stored, str) or not check_password_hash(stored, password):
        print(f"Failed login for {username[:40]!r}")
        return INVALID_CREDENTIALS

    token = get_serializer().dumps({'u': username})
    print(f"Issued a session token for {username} (ttl {SESSION_TTL_SECONDS}s)")
    return {"token": token, "expiresIn": SESSION_TTL_SECONDS}, 200


# ===========================================================================
# Write allowlist
#
# Only these exact paths are writable, and the map is the whole authority —
# a path that is not a literal key here cannot be written, so an
# index-addressed path like experiences.work.2.title is UNREPRESENTABLE
# rather than merely rejected.
#
# That matters because sort_work_items reorders work server-side and
# generateLanguages sorts abilities by star count client-side, so a rendered
# row's index matches nothing in the database. An index-addressed write would
# silently update the wrong record.
#
# The quotes paths ARE index-addressed, and that is safe: quotes is a
# fixed-length array of exactly three, never re-sorted, and the reducer
# backfills all three slots. Order is stable, so index means the same thing on
# both sides.
#
# The four re-sorted arrays (experiences.work, experiences.school,
# abilities.languages, abilities.technologies) are therefore written WHOLE,
# never by index -- see LIST_SCHEMAS below. The client sends the list back in
# the order it was served; the server validates every row and replaces the
# array. No index crosses the wire, so no index can mean the wrong thing.
# ===========================================================================

MAX_FIELD_LENGTH = 4000

# Rows per list. The largest real list is 17 (abilities.technologies), so this
# is ~3.5x headroom and still far below anything that would make the document
# unwieldy. MAX_CONTENT_LENGTH caps the body at 256KB regardless; this exists
# so the error says "too many rows" rather than a generic 413.
MAX_ROWS = 60

# One error per bad row would let a 60-row list generate 60 of them, and
# adminApi.js joins every message into a single string rendered on the page.
MAX_ERRORS = 20

STARS = frozenset(('0', '1', '2', '3', '4', '5'))

ALLOWLIST = frozenset((
    'profile.name',
    'profile.subtitle',
    'profile.description',
    'profile.age',
    'profile.location',
    'links.email',
    'links.linkedin',
    'links.github',
    'quotes.0.quote',
    'quotes.0.by',
    'quotes.1.quote',
    'quotes.1.by',
    'quotes.2.quote',
    'quotes.2.by',
))


# ===========================================================================
# Whole-list writes
#
# THE EXACT KEY SET IS THE POINT, not a formality. A whole-array $set REPLACES
# the array, so a client that echoes back only the keys it renders
# (company/dateLabel/title/body) DELETES isCurrent, startDate and endDate --
# and those three are exactly what sort_work_items orders by. Every sort key
# would collapse to (0, '', ''), the ordering would become a permanent no-op,
# and nothing would look wrong until the next row was added, because a stable
# sort leaves the just-written order alone. Requiring the full key set turns
# that silent, delayed corruption into a 400 at the moment of the write.
#
# It is safe to require it: all four lists are uniform in the live document --
# every school row has exactly these four keys, every work row exactly these
# seven, every ability row exactly these two.
#
# The types are the OTHER half of the job the deleted `isinstance(value, str)`
# check used to do alone. That one line was the whole defence against a Mongo
# operator document reaching $set; a list value has to rebuild it structurally,
# per leaf. Values are copied into fresh dicts below rather than passed through,
# so nothing the caller sent is handed to the driver by reference.
LIST_SCHEMAS = {
    'experiences.school': {
        'company': str, 'dateLabel': str, 'title': str, 'body': str,
    },
    'experiences.work': {
        'company': str, 'dateLabel': str, 'title': str, 'body': str,
        # Not rendered anywhere, and required anyway: sort_work_items reads
        # exactly these three.
        'startDate': str, 'endDate': str, 'isCurrent': bool,
    },
    'abilities.languages': {'ability': str, 'stars': str},
    'abilities.technologies': {'ability': str, 'stars': str},
}


def validate_list(path, value, schema):
    """Return (rows, errors) for one whole-list path. rows is None if any error.

    Errors keep `path` equal to the allowlist path and put the row index in
    `detail`. That is deliberate: src/utils/adminApi.js keys field errors by the
    same dotted path the drafts are keyed by, so an error reported against
    'abilities.languages.2.stars' could never be matched to anything the UI
    holds, and would be dropped on the floor.
    """
    errors = []

    if not isinstance(value, list):
        return None, [{"path": path,
                       "detail": f"must be a list, got {type(value).__name__}"}]
    if not value:
        # An empty list is a section-wipe, and the UI has no gesture that
        # produces one. Refused here so a bug upstream cannot blank a section.
        return None, [{"path": path, "detail": "must not be empty"}]
    if len(value) > MAX_ROWS:
        return None, [{"path": path, "detail": f"more than {MAX_ROWS} rows"}]

    expected = set(schema)
    rows = []

    for index, row in enumerate(value):
        if not isinstance(row, dict):
            errors.append({"path": path,
                           "detail": f"row {index}: must be an object, "
                                     f"got {type(row).__name__}"})
            continue

        got = set(row)
        if got != expected:
            missing = sorted(expected - got)
            unexpected = sorted(str(k)[:40] for k in (got - expected))
            parts = []
            if missing:
                parts.append("missing " + ", ".join(missing))
            if unexpected:
                parts.append("unexpected " + ", ".join(unexpected))
            errors.append({"path": path,
                           "detail": f"row {index}: " + "; ".join(parts)})
            continue

        clean_row = {}
        ok = True
        for key in sorted(expected):
            wanted = schema[key]
            raw = row[key]

            if wanted is bool:
                # isinstance(1, bool) is False, so an int 1 is refused here --
                # which is right: sort_work_items branches on truthiness and a
                # stray 1 would work until someone stored "0", which is truthy.
                if not isinstance(raw, bool):
                    errors.append({"path": path,
                                   "detail": f"row {index}: {key} must be true "
                                             f"or false, got {type(raw).__name__}"})
                    ok = False
                    continue
            else:
                # isinstance(True, str) is False, so a bool cannot slip into a
                # string field even though bool subclasses int.
                if not isinstance(raw, str):
                    errors.append({"path": path,
                                   "detail": f"row {index}: {key} must be a "
                                             f"string, got {type(raw).__name__}"})
                    ok = False
                    continue
                if len(raw) > MAX_FIELD_LENGTH:
                    errors.append({"path": path,
                                   "detail": f"row {index}: {key} is longer "
                                             f"than {MAX_FIELD_LENGTH} characters"})
                    ok = False
                    continue

            clean_row[key] = raw

        if ok and 'stars' in expected and clean_row.get('stars') not in STARS:
            errors.append({"path": path,
                           "detail": f"row {index}: stars must be one of "
                                     f"{', '.join(sorted(STARS))}"})
            ok = False

        if ok:
            # A NEW dict, built from the schema's keys only. Never the caller's
            # object, so nothing unvalidated reaches the driver by reference.
            rows.append(clean_row)

    if errors:
        return None, errors
    return rows, []


def validate_updates(updates):
    """Return (clean, errors). Two kinds of path, validated differently.

    A SCALAR path (ALLOWLIST) takes a plain string. The string check is what
    stops a Mongo operator document reaching $set: {"$ne": null} is a dict, not
    a str, so it is rejected here rather than interpreted by the database.

    A LIST path (LIST_SCHEMAS) takes a whole array of rows, because the four
    array sections are re-sorted -- server-side for experiences.work,
    client-side for both abilities lists -- so a rendered row's index matches
    nothing stored and an index-addressed write would hit the wrong record.
    validate_list rebuilds the same defence structurally, per leaf, and returns
    fresh dicts rather than the caller's objects.

    All or nothing: one bad path rejects the whole batch, so a partially
    applied save is not a state this endpoint can produce.
    """
    errors = []
    clean = {}

    if not isinstance(updates, dict):
        return None, [{"path": "updates", "detail": "must be an object"}]
    if not updates:
        return None, [{"path": "updates", "detail": "must not be empty"}]

    for path, value in updates.items():
        if not isinstance(path, str) or (
            path not in ALLOWLIST and path not in LIST_SCHEMAS
        ):
            errors.append({"path": str(path)[:80], "detail": "not a writable field"})
            continue

        if path in LIST_SCHEMAS:
            rows, row_errors = validate_list(path, value, LIST_SCHEMAS[path])
            if row_errors:
                errors.extend(row_errors)
                continue
            clean[path] = rows
            continue

        if not isinstance(value, str):
            errors.append({"path": path,
                           "detail": f"must be a string, got {type(value).__name__}"})
            continue
        if len(value) > MAX_FIELD_LENGTH:
            errors.append({"path": path,
                           "detail": f"longer than {MAX_FIELD_LENGTH} characters"})
            continue
        clean[path] = value

    if errors:
        return None, errors[:MAX_ERRORS]

    # Asserted rather than assumed: the caller hands `clean` straight to
    # {'$set': clean}, and MongoDB refuses an empty $set with an OperationFailure
    # that the route's broad except would report as a 500 for a request that was
    # in fact merely empty. The `if not updates` guard above makes this
    # unreachable today; it stays so a future path that can validate to nothing
    # cannot reintroduce it silently.
    if not clean:
        return None, [{"path": "updates", "detail": "must not be empty"}]

    return clean, []


# ===========================================================================
# Backups
# ===========================================================================

def write_backup(previous, applied, actor):
    """Snapshot the whole prior document before the write lands.

    Whole document, not a diff: it is ~10KB, and a diff would need a
    reconstruction routine that only ever runs during an incident — i.e.
    never-tested code on the one path where it has to work.

    Restores are deliberately mongosh-only. A restore is a total document
    replacement, exactly the operation the allowlist exists to refuse, so
    exposing one over HTTP would rebuild the unrestricted write path on
    purpose.
    """
    record = {
        'resume_id': previous.get('_id'),
        'created_at': datetime.now(timezone.utc),
        'actor': actor,
        'changed_paths': sorted(applied.keys()),
        'previous': previous,
    }
    backups().insert_one(record)


def prune_backups(resume_id):
    """Trim to the newest BACKUP_KEEP generations.

    Best effort on purpose, and called AFTER the write: the snapshot is the
    safety property, this is housekeeping. A prune that raised would otherwise
    be caught by the caller's broad except and reported as a failed write —
    for a document that had in fact been saved.
    """
    try:
        keep = [b['_id'] for b in backups()
                .find({'resume_id': resume_id}, {'_id': 1})
                .sort('created_at', DESCENDING)
                .limit(BACKUP_KEEP)
                if b.get('_id') is not None]
        if keep:
            backups().delete_many({'resume_id': resume_id, '_id': {'$nin': keep}})
    except Exception as e:
        print(f"Backup prune failed (the write itself succeeded): {str(e)}")


# ===========================================================================
# Routes
# ===========================================================================

def public_view(resume):
    """One shaping function, shared by read and write, so a PUT cannot return
    200 for a document the next GET would 500 on."""
    resume.pop('_id', None)
    work = resume.get('experiences', {}).get('work')
    if isinstance(work, list):
        resume['experiences']['work'] = sort_work_items(work)
    return resume


@app.route('/getResume', methods=['GET'])
def get_resume():
    print("GET /getResume endpoint hit")
    try:
        # _id projected out: nothing in the frontend reads it, and it keeps a
        # {"$oid": ...} extended-JSON blob out of the public response.
        resume = resumes().find_one({}, {'_id': 0})
        if resume is None:
            return {"error": "No resume found in database"}, 404
        return jsonify(public_view(resume))
    except Exception as e:
        print(f"Database error: {str(e)}")
        return SERVER_ERROR


@app.route('/updateResume', methods=['PUT'])
@require_session
def update_resume():
    print("PUT /updateResume endpoint hit")

    # The auth contract, asserted rather than assumed: if the decorator is
    # ever removed, this refuses instead of writing unauthenticated.
    actor = getattr(g, 'admin_username', None)
    if not isinstance(actor, str) or not actor:
        print("require_session did not set g.admin_username - refusing write")
        return NOT_CONFIGURED

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return {"error": "Expected a JSON object body", "code": "bad_request"}, 400

    clean, errors = validate_updates(body.get('updates'))
    if errors:
        return {"error": "Some fields could not be written",
                "code": "validation_failed", "errors": errors}, 400

    try:
        previous = resumes().find_one()
        if previous is None:
            return {"error": "No resume found in database"}, 404

        write_backup(previous, clean, actor)

        # Scoped by _id, never an empty filter.
        result = resumes().update_one({'_id': previous['_id']}, {'$set': clean})
        if result.matched_count == 0:
            return {"error": "No resume found in database"}, 404

        prune_backups(previous['_id'])

        updated = resumes().find_one({}, {'_id': 0})
        print(f"{actor} updated {', '.join(sorted(clean))}")
        return jsonify(public_view(updated)), 200

    except Exception as e:
        print(f"Update error: {str(e)}")
        return SERVER_ERROR


if __name__ == '__main__':
    app.run(debug=True)
