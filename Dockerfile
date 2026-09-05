FROM python:3.9-slim

WORKDIR /

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# The commit this image was built from, so a running container can say which
# code it is actually serving. Everything else about a build looks identical
# from outside -- which is how an API container sat on a stale image through
# four site deploys, answering every probe exactly as the new one would.
#
# An ARG with a default, not a required one: a local `docker build` with no
# --build-arg still works and simply reports "unknown". CI passes the real SHA.
# It goes in AFTER `COPY . .` on purpose -- an ARG that changes every commit
# would otherwise invalidate the dependency layer above it and reinstall pip on
# every build.
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA

# Make port 5000 available outside the container
EXPOSE 5000

# Serve with gunicorn bound to all interfaces so the frontend's nginx can
# reach it from a sibling container.
#
# Both previous CMDs were broken, which is why the running container must
# already override this:
#   python -m server run --host=0.0.0.0
#     -> trailing args were never parsed; fell through to __main__ and ran
#        app.run(debug=True), binding 127.0.0.1 with the debugger enabled
#   flask -app server run --host=0.0.0.0 --port=5000
#     -> single-dash "-app" is not an option; flask exits with
#        "Error: No such option: -a"
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "60", "server:app"]
