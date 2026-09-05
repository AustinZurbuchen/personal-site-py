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

- `server.py` — four routes (`GET /getResume`, `GET /version`, `POST /session`,
  `PUT /updateResume`), DB access, serialization.

`GET /version` reports the commit baked into the image by the Dockerfile's
`GIT_SHA` ARG. It exists because a deploy was unverifiable: every other response
is byte-identical across builds, so an API container left on a stale image
answers every probe exactly as a fresh one would. One sat that way through four
site deploys, and the first sign was a save failing with "not a writable field"
for a path the current code allows. Unauthenticated and GET on purpose —
needing a password to check a deploy is what let that one go unchecked — and it
touches no database, so it can tell a bad deploy apart from a bad Atlas.
`test_only_the_expected_routes_exist` pins the route set, so a fifth route is
added deliberately or not at all.
- `test_server.py` — 64 tests, no network. Run `python -m pytest -q`.
  `.dockerignore` keeps it out of the image.
- `utils.py` — `sort_work_items`, ordering work experience current-first then
  by date. Applied in `GET /getResume`.
- Collections: `resumes` (one document), `admins` (one document per admin,
  field is `password_hash` and never `password`), and `resume_backups` (one
  document per applied write, capped at 50 generations).

**Writes go through one allowlisted endpoint.** `PUT /updateResume` accepts
`{"updates": {"<dotted path>": "<string>"}}` where every path must be a literal
key in `ALLOWLIST`. That makes an index-addressed path like
`experiences.work.2.title` **unrepresentable**, which matters: `sort_work_items`
reorders work server-side and `generateLanguages` sorts abilities client-side,
so a rendered row's index matches nothing in the database and an index-addressed
write would silently update the wrong record.

The `quotes.N.*` paths are index-addressed and that is safe — `quotes` is a
fixed array of exactly three, never re-sorted, and the reducer backfills all
three slots. The four re-sorted arrays are absent from the scalar allowlist and are
written **whole**, via `LIST_SCHEMAS` — the client sends the list back in the
order it was served and the server validates every row and replaces the array,
so no index ever crosses the wire.

`LIST_SCHEMAS` requires the **exact** key set per row, including the three
`experiences.work` keys nothing renders (`startDate`, `endDate`, `isCurrent`).
That is not pedantry: a whole-array `$set` replaces the array, so a client
echoing back only what it renders would delete those three — and they are
precisely what `sort_work_items` orders by. Every sort key would collapse to
`(0, '', '')`, the ordering would become a permanent no-op, and nothing would
look wrong until the next row was added, because a stable sort leaves the
just-written order alone. Requiring the full key set turns silent, delayed
corruption into a 400 at the moment of the write.

Requiring the exact key set is safe because all four lists are uniform in the
live document. If that ever stops being true, the fix is to widen the schema
deliberately, not to relax the check to a subset.

**`ADMIN_TOKEN` and `require_token` are gone**, replaced by `require_session`.
A door is only as strong as the weakest credential it accepts, and a
non-expiring shared secret beside the session token would have made the real
security one string in a `.env`. Remove `ADMIN_TOKEN` from the container's
variables.

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

This service is on the public internet. Write routes are gated by
`@require_session`, which validates a signed, expiring token carrying a
username. (An earlier version of this file described a shared `ADMIN_TOKEN`
and `require_token`; both are gone.)

- **`PUT /updateResume` is guarded by `@require_session`.** It requires
  `Authorization: Bearer <session token>`, minted by `POST /session` against a
  scrypt hash in the `admins` collection, and scopes its write by `_id` rather
  than an empty filter. The guard **fails closed**: with
  `ADMIN_SESSION_SECRET` unset it returns 503 rather than running
  unauthenticated. Never "fix" that 503 by removing the decorator — set the env
  var. Apply `@require_session` to every future write route.
- The bytes-not-str lesson from the old `require_token` still applies to any
  future secret comparison: `hmac.compare_digest` raises `TypeError` on a str
  containing non-ASCII, which turns an unauthenticated request into a 500
  instead of a 401. Encode both sides.
- **The public API is read-only by proof, not convention.** The frontend's
  `nginx.conf` wraps `location /api/` in `limit_except GET HEAD { deny all; }`,
  so no non-GET method reaches Flask from the internet regardless of any
  application bug. The admin vhost is a separate server block on port 8081,
  bound to the LAN and never published through Nginx Proxy Manager, and does
  not carry that restriction. Do not remove it to
  "make writes work" — that is what the admin vhost is for.
- Credentials come from `.env` (`DBUSER`, `DBPASS`) and are interpolated into
  the connection string. `.env` is gitignored; keep it that way and never echo
  those values into logs or output.
- No rate limiting anywhere. `PUT /updateResume` validates its body against
  the allowlist and the list schemas; no other route validates input.

When adding a write endpoint, it needs `@require_session`, a scoped filter
(never `{}`), and validation of the request body — in that order.

Config lives in `.env` (gitignored); `.env.example` documents the required
keys. Adding a new one means updating `.env.example` too.

## Tests

`python -m pytest -q` runs 99 cases in `test_server.py`, with **no network and
no Atlas access**. `server.py` builds its `MongoClient` lazily inside
`get_client()`, and that is the seam: the `db` fixture replaces the three
collection accessors and monkeypatches `get_client` to raise. A test that
reached the network would hang rather than fail, so the fake asserts it is the
thing being used. Never write a test that bypasses that fixture — an earlier
draft of the update tests paired a valid token with a valid body against the
real cluster and was only stopped by missing local CA certificates.

The bulk of the suite is the write path: session minting and expiry, the
allowlist, and the `LIST_SCHEMAS` row validation described above.

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

## Conventions

Routes are `@app.route('/camelCaseName', methods=['GET'])` with a
`print(...)` at the top of each handler for NAS log visibility. Return
`(dict, status_code)` tuples for errors, bare dicts for success. Keep new
routes consistent with that until there is a reason to restructure.
