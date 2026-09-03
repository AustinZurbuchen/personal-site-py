# personal-site-py

Flask API backing austinzurbuchen.com. Serves resume content from MongoDB
Atlas to the `personal-site` React frontend (separate repo,
`../personal-site`).

## Stack

Flask · PyMongo → MongoDB Atlas (`personalsite.qbhpviu.mongodb.net`, db `test`)
· flask-cors · python-dotenv. Everything lives in `server.py`. Runs in Docker
on a Synology NAS as a sibling container to the frontend.

## Layout

- `server.py` — routes, DB access, serialization.
- `utils.py` — `sort_work_items`, ordering work experience current-first then
  by date. Applied in `GET /getResume`.
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
Docker network. That network is `zurbnet`, declared `external: true` in both
repos' `docker-compose.yml`. It is created outside compose
(`docker network create zurbnet`), so compose will not recreate it and
`docker compose up` fails fast if it is missing rather than silently
attaching to a private network nginx cannot reach.

Renaming the container breaks the proxy with no build-time warning — the
`container_name` in `docker-compose.yml` is load-bearing.

Because of that proxy the API is same-origin in production, so CORS is not
exercised there at all. `CORS(...)` is scoped to `http://localhost:3000` and
`http://127.0.0.1:3000` with `supports_credentials=True`, which matters only
for local dev against a remote API. Widening it would not help production and
would expose the API to any origin.

`docker-compose.yml` builds this service as `personal-site-py` (the
`container_name` is load-bearing — nginx resolves it) and loads secrets from
`.env` at container start via `env_file`. `.dockerignore` keeps `.env` out of
the build context, so **secrets are no longer baked into the image** and the
container will start without credentials if `.env` is missing beside the
compose file. `Dockerfile` serves the app with gunicorn bound to
`0.0.0.0:5000`.

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

- `requirements.txt` pins only `pymongo>=4.6`; everything else floats, so a
  rebuild pulls whatever is current. That is already true of the running
  image; pinning the rest would reduce the risk.
- `app.run(debug=True)` remains in `__main__` for local runs. The container
  no longer uses it (gunicorn serves the app), but never start the container
  that way — it binds loopback and enables the interactive debugger.
- The image runs as root and `hello.py` is still copied into it.
- Route handlers other than `/getResume` have no error handling — a missing
  key raises and returns a 500 with a stack trace under debug.
- No tests.

## Conventions

Routes are `@app.route('/camelCaseName', methods=['GET'])` with a
`print(...)` at the top of each handler for NAS log visibility. Return
`(dict, status_code)` tuples for errors, bare dicts for success. Keep new
routes consistent with that until there is a reason to restructure.
