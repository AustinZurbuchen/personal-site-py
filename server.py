import os
import hmac
from functools import wraps
from dotenv import main
from urllib.parse import quote_plus
from pymongo import MongoClient
from flask import Flask, request
from bson import json_util
from flask_cors import CORS
from utils import sort_work_items
import json

main.load_dotenv()
db_user = os.getenv('DBUSER')
db_pass = os.getenv('DBPASS')
admin_token = (os.getenv('ADMIN_TOKEN') or '').strip()
app = Flask(__name__)
CORS(app, origins=["http://localhost:3000", "http://127.0.0.1:3000"], supports_credentials=True)

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


def jsonify(text):
    return json.loads(json_util.dumps(text))

def require_token(handler):
    """Gate a route behind the ADMIN_TOKEN bearer token.

    Fails closed: if ADMIN_TOKEN is unset the route returns 503 rather than
    running, so a missing env var can never silently reopen a write endpoint.
    """
    @wraps(handler)
    def wrapper(*args, **kwargs):
        if not admin_token:
            print("ADMIN_TOKEN is not set - refusing write request")
            return {"error": "Server is not configured for writes"}, 503

        header = request.headers.get('Authorization', '')
        if not header.startswith('Bearer '):
            return {"error": "Unauthorized"}, 401

        # Compare as BYTES. hmac.compare_digest raises TypeError on a str
        # containing non-ASCII, so comparing the raw header turned an
        # unauthenticated request with a non-ASCII bearer token into a 500
        # instead of a 401. Encoding first keeps every rejection a 401 and
        # keeps the comparison constant-time.
        presented = header[len('Bearer '):].strip()
        if not hmac.compare_digest(presented.encode('utf-8'),
                                   admin_token.encode('utf-8')):
            return {"error": "Unauthorized"}, 401

        return handler(*args, **kwargs)
    return wrapper

@app.route('/getResume', methods=['GET'])
def get_resume():
    print("GET /getResume endpoint hit")
    try:
        # _id projected out: nothing in the frontend reads it, and it keeps a
        # {"$oid": ...} extended-JSON blob out of the public response.
        resume = resumes().find_one({}, {'_id': 0})
        if resume is None:
            return {"error": "No resume found in database"}, 404
        resume['experiences']['work'] = sort_work_items(resume['experiences']['work'])
        return jsonify(resume)
    except Exception as e:
        print(f"Database error: {str(e)}")
        return {"error": "Database connection failed"}, 500
    
if __name__ == '__main__':
    app.run(debug=True)