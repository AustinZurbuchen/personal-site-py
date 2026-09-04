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
# abilities.languages, abilities.technologies) are deliberately absent. They
# arrive in stage 4, written whole, never by index.
# ===========================================================================

MAX_FIELD_LENGTH = 4000

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


def validate_updates(updates):
    """Return (clean, errors). Every value must be a plain string.

    The string check is what stops a Mongo operator document reaching $set:
    {"$ne": null} is a dict, not a str, so it is rejected here rather than
    interpreted by the database.
    """
    errors = []
    clean = {}

    if not isinstance(updates, dict):
        return None, [{"path": "updates", "detail": "must be an object"}]
    if not updates:
        return None, [{"path": "updates", "detail": "must not be empty"}]

    for path, value in updates.items():
        if not isinstance(path, str) or path not in ALLOWLIST:
            errors.append({"path": str(path)[:80], "detail": "not a writable field"})
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

    return (None, errors) if errors else (clean, [])


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
