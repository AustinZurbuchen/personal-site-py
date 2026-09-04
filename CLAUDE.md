# personal-site-py

Flask API backing austinzurbuchen.com. Serves resume content from MongoDB
Atlas to the `personal-site` React frontend (separate repo,
`../personal-site`).

## Stack

Flask · PyMongo → MongoDB Atlas (`personalsite.qbhpviu.mongodb.net`, db `test`)
· flask-cors · python-dotenv. Everything lives in `server.py`. Runs in Docker
on an Unraid NAS as a sibling container to the frontend.

**Deploys are registry-based.** The container is managed by Unraid's Docker
Manager (label `net.unraid.docker.managed: dockerman`), which pulls a
published image — there is no repo checkout or compose project on the NAS.
`.github/workflows/publish.yml` builds `linux/amd64` on push to `master` and
pushes to GHCR; the NAS pulls it. `docker-compose.yml` documents the topology
and is useful locally, but is **not** what runs in production, so a change
there does not reach the NAS on its own.

## Layout

- `server.py` — one route (`GET /getResume`), DB access, serialization.
- `utils.py` — `sort_work_items`, ordering work experience current-first then
  by date. Applied in `GET /getResume`.
- Collections: `resumes`, holding a single document.

**The API surface is deliberately one read-only route.** Five other GET routes,
`PUT /updateTest` and `POST /login` were deleted once an audit confirmed the
frontend calls only `/getResume`; `/getExperiences` had already drifted to
returning work unsorted. `require_token` is kept unused on purpose — the
planned `PUT /updateResume` uses it.

`jsonify()` here is a local helper wrapping `bson.json_util`, **not**
`flask.jsonify`. It passes Mongo extended JSON straight through, so responses
include `_id` as `{"$oid": "..."}`. The frontend tolerates this; changing the
shape is a breaking change.

`GET /getResume` projects `_id` out. Nothing in the frontend reads it, and it
keeps a `{"$oid": ...}` extended-JSON blob out of the public response.

**The MongoClient is built lazily, once, by `get_client()`.** Do not move it
back to module scope. A `mongodb+srv://` URI resolves DNS inside the
constructor, so at import a DNS failure raises during app load — and a gunicorn
worker that dies loading the app exits 3, which the arbiter treats as fatal for
the whole master. A transient blip at NAS boot would kill the container rather
than one request. Built lazily, the same fault becomes a catchable 500 and the
next request recovers. `connect=False` defers the SRV lookup as well.

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

- **`PUT /updateTest` is guarded by a bearer token.** The `require_token`
  decorator requires `Authorization: Bearer $ADMIN_TOKEN` and scopes its write
  by `_id` rather than the empty filter it used to use. The comparison is
  `hmac.compare_digest` over **bytes**, not str: that function raises
  `TypeError` on a str containing non-ASCII, so comparing the raw header turned
  an unauthenticated request into a 500 instead of a 401. Encode both sides.
  The guard **fails closed**: with `ADMIN_TOKEN` unset the route returns 503
  instead of running unauthenticated. Never "fix" that 503 by removing the decorator — set the
  env var. Apply `@require_token` to every future write route.
- **The public API is read-only by proof, not convention.** The frontend's
  `nginx.conf` wraps `location /api/` in `limit_except GET HEAD { deny all; }`,
  so no non-GET method reaches Flask from the internet regardless of any
  application bug. The planned admin vhost is a separate server block on an
  unpublished port and does not carry that restriction. Do not remove it to
  "make writes work" — that is what the admin vhost is for.
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
- The image runs as root.
- `print()` is block-buffered under Docker and there is no `PYTHONUNBUFFERED`,
  so the per-handler log convention below does not actually reach the NAS log
  promptly.
- The base image is Python 3.9, end-of-life since October 2025.
- No tests.

## Conventions

Routes are `@app.route('/camelCaseName', methods=['GET'])` with a
`print(...)` at the top of each handler for NAS log visibility. Return
`(dict, status_code)` tuples for errors, bare dicts for success. Keep new
routes consistent with that until there is a reason to restructure.
