# personal-site-py

Flask API backing austinzurbuchen.com. Serves resume content from MongoDB
Atlas to the `personal-site` React frontend (separate repo,
`../personal-site`).

## Stack

Flask · PyMongo → MongoDB Atlas (`personalsite.qbhpviu.mongodb.net`, db `test`)
· flask-cors · python-dotenv. Everything lives in `server.py`. Runs in Docker
on a Synology NAS as a sibling container to the frontend.

## Layout

- `server.py` — the entire application. Routes, DB access, serialization.
- `hello.py` — a leftover scaffold, unused.
- Collections: `resumes` (single document, read by every GET) and `admins`.

`jsonify()` here is a local helper wrapping `bson.json_util`, **not**
`flask.jsonify`. It passes Mongo extended JSON straight through, so responses
include `_id` as `{"$oid": "..."}`. The frontend tolerates this; changing the
shape is a breaking change.

Every GET calls `db.find_one()` with no filter and returns a subtree of the
one resume document.

## How it is reached

The frontend's nginx proxies `/api/` → `http://personal-site-py:5000/`, so the
container must be resolvable under the hostname `personal-site-py` on a shared
Docker network. That network is **not** declared in the frontend's
`docker-compose.yml` — it is wired up manually on the NAS. Renaming the
container or changing the network breaks the site with no build-time warning.

Because of that proxy the API is same-origin in production, so the wide-open
`CORS(app)` is not actually load-bearing.

## Security posture — read before touching routes

This service is on the public internet. Write routes are gated by a shared
bearer token; there is no per-user authentication.

- **`PUT /updateTest` is guarded by a bearer token.** It requires
  `Authorization: Bearer $ADMIN_TOKEN`, compared with `hmac.compare_digest`
  by the `require_token` decorator, and scopes its write by `_id` rather than
  the empty filter it used to use. The guard **fails closed**: with
  `ADMIN_TOKEN` unset the route returns 503 instead of running
  unauthenticated. Never "fix" that 503 by removing the decorator — set the
  env var. Apply `@require_token` to every future write route.
- **`POST /login` compares passwords in plaintext** (`admin['password'] !=
  data['password']`) and issues no token, cookie, or session on success — the
  caller just gets `{"status": "Success"}`. It is not a usable auth mechanism
  and must not be treated as one. It is also not currently deployed
  (`/api/login` returns 404 in production).
- Credentials come from `.env` (`DBUSER`, `DBPASS`) and are interpolated into
  the connection string. `.env` is gitignored; keep it that way and never echo
  those values into logs or output.
- No rate limiting anywhere. `PUT /updateTest` validates its body; no other
  route validates input.

When adding a write endpoint, it needs `@require_token`, a scoped filter
(never `{}`), and validation of the request body — in that order.

Config lives in `.env` (gitignored); `.env.example` documents the required
keys. Adding a new one means updating `.env.example` too.

## Known issues

- **The Dockerfile `CMD` is wrong**: `python -m server run --host=0.0.0.0` is
  not a valid invocation of this app. Should be gunicorn, or at minimum
  `flask --app server run`.
- `requirements.txt` pins nothing — builds are not reproducible.
- `app.run(debug=True)` in `__main__`; debug must never reach production.
- Route handlers other than `/getResume` have no error handling — a missing
  key raises and returns a 500 with a stack trace under debug.
- No tests.

## Conventions

Routes are `@app.route('/camelCaseName', methods=['GET'])` with a
`print(...)` at the top of each handler for NAS log visibility. Return
`(dict, status_code)` tuples for errors, bare dicts for success. Keep new
routes consistent with that until there is a reason to restructure.
