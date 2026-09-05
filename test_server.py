"""Tests for the admin session and write endpoints.

Runs with NO network and NO Atlas access. server.py builds its MongoClient
lazily inside get_client(), which is the seam: the fixture replaces the three
collection accessors, so no client is ever constructed and nothing resolves
DNS. A test that reached the network would hang, not fail, so the fake also
asserts it is the thing being used.

Run:  python -m pytest -q
"""
import os
import copy

import pytest

os.environ.setdefault('ADMIN_SESSION_SECRET', 'k' * 40)
os.environ.setdefault('DBUSER', 'testuser')
os.environ.setdefault('DBPASS', 'testpass')

import server  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

PASSWORD = 'correct horse battery staple'

RESUME = {
    '_id': 'resume-1',
    'profile': {'name': 'Austin Zurbuchen', 'subtitle': 'Passionate Software Developer',
                'description': 'BEFORE', 'age': '30 years', 'location': 'Folsom, California'},
    'experiences': {
        'school': [{'company': 'SJSU', 'dateLabel': 'May 2018', 'title': 'BS', 'body': '.'}],
        'work': [
            {'company': 'BeyondID', 'dateLabel': 'a', 'title': 't', 'body': 'b',
             'isCurrent': False, 'startDate': '2021', 'endDate': '2022'},
            {'company': 'Ambii', 'dateLabel': 'c', 'title': 'u', 'body': 'v',
             'isCurrent': False, 'startDate': '2017', 'endDate': '2021'},
        ],
    },
    'abilities': {'languages': [{'ability': 'ReactJS', 'stars': '5'}],
                  'technologies': [{'ability': 'Git', 'stars': '5'}]},
    'quotes': [{'quote': 'q0', 'by': '- a'}, {'quote': 'q1', 'by': '- b'},
               {'quote': 'q2', 'by': '- c'}],
    'links': {'email': 'e@example.com', 'linkedin': 'https://l', 'github': 'https://g'},
}


class FakeCursor(list):
    def sort(self, *a, **k):
        return self

    def limit(self, n):
        return FakeCursor(self[:n])


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs if docs is not None else []
        self.inserted = []
        self.deleted = []

    def find_one(self, flt=None, projection=None):
        for d in self.docs:
            if not flt or all(d.get(k) == v for k, v in flt.items()):
                out = copy.deepcopy(d)
                if projection and projection.get('_id') == 0:
                    out.pop('_id', None)
                return out
        return None

    def find(self, flt=None, projection=None):
        return FakeCursor([copy.deepcopy(d) for d in self.docs])

    def update_one(self, flt, update):
        target = None
        for d in self.docs:
            if all(d.get(k) == v for k, v in flt.items()):
                target = d
                break

        class Result:
            matched_count = 1 if target else 0
            modified_count = 1 if target else 0

        if target:
            for path, value in update.get('$set', {}).items():
                node = target
                parts = path.split('.')
                for seg in parts[:-1]:
                    node = node[int(seg)] if seg.isdigit() else node[seg]
                key = parts[-1]
                if isinstance(node, list):
                    node[int(key)] = value
                else:
                    node[key] = value
        return Result()

    def insert_one(self, doc):
        # Real Mongo assigns _id on insert; the fake must too, or the prune
        # reads a key that only exists in production.
        doc.setdefault('_id', f'gen-{len(self.docs)}')
        self.inserted.append(copy.deepcopy(doc))
        self.docs.append(doc)
        return type('R', (), {'inserted_id': doc['_id']})()

    def delete_many(self, flt):
        self.deleted.append(flt)
        return type('R', (), {'deleted_count': 0})()


@pytest.fixture
def db(monkeypatch):
    resumes = FakeCollection([copy.deepcopy(RESUME)])
    admins = FakeCollection([{
        'username': 'austin',
        'password_hash': generate_password_hash(PASSWORD, method='scrypt'),
    }])
    backups = FakeCollection([])

    monkeypatch.setattr(server, 'resumes', lambda: resumes)
    monkeypatch.setattr(server, 'admins', lambda: admins)
    monkeypatch.setattr(server, 'backups', lambda: backups)

    def boom():
        raise AssertionError('server.py constructed a real MongoClient')
    monkeypatch.setattr(server, 'get_client', boom)

    return {'resumes': resumes, 'admins': admins, 'backups': backups}


@pytest.fixture
def client():
    server.app.testing = True
    return server.app.test_client()


def login(client, password=PASSWORD, username='austin'):
    return client.post('/session', json={'username': username, 'password': password})


def auth(client):
    return {'Authorization': 'Bearer ' + login(client).get_json()['token']}


# ---------------------------------------------------------------- sessions

def test_correct_credentials_return_a_token(client, db):
    r = login(client)
    assert r.status_code == 200
    assert r.get_json()['token']
    assert r.get_json()['expiresIn'] == server.SESSION_TTL_SECONDS


@pytest.mark.parametrize('password', ['wrong', '', PASSWORD + ' ', PASSWORD.upper()])
def test_wrong_password_is_rejected(client, db, password):
    assert login(client, password=password).status_code in (400, 401)


def test_unknown_username_is_rejected(client, db):
    assert login(client, username='nobody').status_code == 401


def test_the_same_error_for_bad_user_and_bad_password(client, db):
    """Neither response may reveal whether the username exists."""
    a = login(client, username='nobody').get_json()
    b = login(client, password='wrong').get_json()
    assert a == b


@pytest.mark.parametrize('body', [None, [], 'x', 42, {}, {'username': 'austin'},
                                  {'username': None, 'password': 'x'},
                                  {'username': 'a', 'password': {'$ne': None}}])
def test_malformed_login_bodies_are_rejected_not_crashed(client, db, body):
    assert client.post('/session', json=body).status_code in (400, 401)


def test_a_password_longer_than_the_cap_never_reaches_scrypt(client, db):
    r = client.post('/session', json={'username': 'austin',
                                      'password': 'x' * (server.MAX_PASSWORD_LENGTH + 1)})
    assert r.status_code == 400


def test_a_plaintext_password_field_does_not_authenticate(client, db):
    """The collection this replaced stored `password`. If a stale document
    survives the migration it must not log anyone in."""
    db['admins'].docs[0] = {'username': 'austin', 'password': PASSWORD}
    assert login(client).status_code == 401


# ------------------------------------------------------------------- auth

@pytest.mark.parametrize('headers', [
    {},
    {'Authorization': ''},
    {'Authorization': 'Bearer'},
    {'Authorization': 'Bearer '},
    {'Authorization': 'Bearer nonsense'},
    {'Authorization': 'Basic ' + 'x' * 20},
    {'Authorization': 'bearer lowercase-scheme'},
    {'Authorization': 'Bearer toké'},          # non-ASCII must 401, never 500
    {'Authorization': 'Bearer tok\U0001f600'},
])
def test_every_bad_credential_is_a_401(client, db, headers):
    r = client.put('/updateResume', json={'updates': {'profile.description': 'x'}},
                   headers=headers)
    assert r.status_code == 401


def test_a_token_signed_with_another_key_is_rejected(client, db):
    from itsdangerous import URLSafeTimedSerializer
    forged = URLSafeTimedSerializer('a' * 40, salt=server.SESSION_SALT).dumps({'u': 'austin'})
    r = client.put('/updateResume', json={'updates': {'profile.description': 'x'}},
                   headers={'Authorization': 'Bearer ' + forged})
    assert r.status_code == 401


def test_an_expired_token_is_rejected(client, db, monkeypatch):
    token = login(client).get_json()['token']
    monkeypatch.setattr(server, 'SESSION_TTL_SECONDS', -1)
    r = client.put('/updateResume', json={'updates': {'profile.description': 'x'}},
                   headers={'Authorization': 'Bearer ' + token})
    assert r.status_code == 401
    assert r.get_json()['code'] == 'session_expired'


def test_writes_fail_closed_when_no_secret_is_configured(client, db, monkeypatch):
    monkeypatch.setattr(server, 'SESSION_SECRET', '')
    r = client.put('/updateResume', json={'updates': {'profile.description': 'x'}},
                   headers={'Authorization': 'Bearer anything'})
    assert r.status_code == 503


def test_a_short_secret_is_treated_as_unconfigured(client, db, monkeypatch):
    monkeypatch.setattr(server, 'SESSION_SECRET', 'tooshort')
    assert login(client).status_code == 503


def test_the_legacy_admin_token_path_is_gone():
    assert not hasattr(server, 'require_token')
    assert not hasattr(server, 'ADMIN_TOKEN')


# ------------------------------------------------------------- rejections

def test_a_rejected_request_writes_nothing(client, db):
    """The test that matters most: every refusal must leave the document and
    the backup collection untouched."""
    before = copy.deepcopy(db['resumes'].docs[0])
    for headers, payload in [
        ({}, {'updates': {'profile.description': 'HACKED'}}),
        ({'Authorization': 'Bearer bad'}, {'updates': {'profile.description': 'HACKED'}}),
        (auth(client), {'updates': {'profile.secret': 'HACKED'}}),
        (auth(client), {'updates': {'experiences.work.0.title': 'HACKED'}}),
        (auth(client), {'updates': {'profile.description': {'$ne': None}}}),
        (auth(client), {'updates': {}}),
    ]:
        client.put('/updateResume', json=payload, headers=headers)
    assert db['resumes'].docs[0] == before
    assert db['backups'].inserted == []


@pytest.mark.parametrize('path', [
    'experiences.work.0.title',      # the index hazard, in its exact form
    'experiences.work',              # whole arrays arrive in stage 4
    'abilities.languages',
    'profile',                       # a parent object
    'profile.secret',
    '__v',
    '_id',
    'quotes.3.quote',                # out of range
    'quotes.0',
    '$where',
    'profile.description.$set',
])
def test_paths_outside_the_allowlist_are_refused(client, db, path):
    r = client.put('/updateResume', json={'updates': {path: 'x'}}, headers=auth(client))
    assert r.status_code == 400
    assert r.get_json()['code'] == 'validation_failed'
    assert r.get_json()['errors'][0]['path'] == path


@pytest.mark.parametrize('value', [None, 42, 3.5, True, [], {}, {'$ne': None},
                                   ['a'], {'a': 'b'}])
def test_non_string_values_are_refused(client, db, value):
    r = client.put('/updateResume', json={'updates': {'profile.description': value}},
                   headers=auth(client))
    assert r.status_code == 400


def test_an_overlong_value_is_refused(client, db):
    r = client.put('/updateResume',
                   json={'updates': {'profile.description':
                                     'x' * (server.MAX_FIELD_LENGTH + 1)}},
                   headers=auth(client))
    assert r.status_code == 400


def test_one_bad_path_rejects_the_whole_batch(client, db):
    before = db['resumes'].docs[0]['profile']['description']
    r = client.put('/updateResume',
                   json={'updates': {'profile.description': 'GOOD',
                                     'profile.secret': 'BAD'}},
                   headers=auth(client))
    assert r.status_code == 400
    assert db['resumes'].docs[0]['profile']['description'] == before


# ------------------------------------------------------------- happy path

def test_a_valid_edit_is_written(client, db):
    r = client.put('/updateResume', json={'updates': {'profile.description': 'AFTER'}},
                   headers=auth(client))
    assert r.status_code == 200
    assert db['resumes'].docs[0]['profile']['description'] == 'AFTER'


def test_the_response_is_the_same_shape_getresume_returns(client, db):
    put = client.put('/updateResume', json={'updates': {'profile.description': 'AFTER'}},
                     headers=auth(client)).get_json()
    get = client.get('/getResume').get_json()
    assert put == get
    assert '_id' not in put


def test_every_allowlisted_path_is_actually_writable(client, db):
    """Guards against a path being listed but unreachable — the allowlist and
    the document shape must agree."""
    for path in sorted(server.ALLOWLIST):
        r = client.put('/updateResume', json={'updates': {path: 'value'}},
                       headers=auth(client))
        assert r.status_code == 200, f'{path} -> {r.get_json()}'


def test_several_fields_write_together(client, db):
    r = client.put('/updateResume',
                   json={'updates': {'profile.name': 'A', 'links.email': 'b@c.d',
                                     'quotes.1.by': '- Z'}},
                   headers=auth(client))
    assert r.status_code == 200
    doc = db['resumes'].docs[0]
    assert doc['profile']['name'] == 'A'
    assert doc['links']['email'] == 'b@c.d'
    assert doc['quotes'][1]['by'] == '- Z'


# --------------------------------------------------------------- backups

def test_a_backup_is_written_before_the_document_changes(client, db):
    client.put('/updateResume', json={'updates': {'profile.description': 'AFTER'}},
               headers=auth(client))
    assert len(db['backups'].inserted) == 1
    snapshot = db['backups'].inserted[0]
    assert snapshot['previous']['profile']['description'] == 'BEFORE'
    assert snapshot['actor'] == 'austin'
    assert snapshot['changed_paths'] == ['profile.description']


def test_the_backup_prune_keeps_the_newest_generations(client, db):
    client.put('/updateResume', json={'updates': {'profile.description': 'x'}},
               headers=auth(client))
    assert db['backups'].deleted, 'prune never ran'
    flt = db['backups'].deleted[-1]
    assert '$nin' in flt['_id']


# ----------------------------------------------------------------- shape

def test_getresume_still_sorts_work_and_hides_id(client, db):
    body = client.get('/getResume').get_json()
    assert '_id' not in body
    assert [w['company'] for w in body['experiences']['work']] == ['BeyondID', 'Ambii']


def test_errors_are_json_not_html(client, db):
    r = client.post('/getResume')            # 405
    assert r.status_code == 405
    assert r.is_json


def test_only_the_expected_routes_exist():
    """An exact set, so a route is exposed on purpose or not at all.

    /version was added deliberately and this line updated with it. Anything
    arriving here without that edit is an endpoint nobody decided to publish.
    """
    rules = {r.rule for r in server.app.url_map.iter_rules() if r.endpoint != 'static'}
    assert rules == {'/getResume', '/session', '/updateResume', '/version'}


def test_every_write_route_is_guarded():
    """Auth is usually lost by adding a route, not by breaking one."""
    for rule in server.app.url_map.iter_rules():
        methods = rule.methods - {'HEAD', 'OPTIONS'}
        if rule.endpoint == 'static' or methods <= {'GET'}:
            continue
        if rule.rule == '/session':
            continue
        fn = server.app.view_functions[rule.endpoint]
        assert getattr(fn, '__wrapped__', None) is not None, f'{rule.rule} is unguarded'


# ------------------------------------------------------- whole-list writes
#
# The four array sections are written whole because they are re-sorted --
# experiences.work by sort_work_items on the server, both abilities lists by
# generateLanguages/generateTechnologies on the client -- so a rendered row's
# index matches nothing stored. No index crosses the wire, so these tests are
# about the SHAPE of a list rather than about addressing one row.

WORK_ROW = {'company': 'BeyondID', 'dateLabel': 'a', 'title': 't', 'body': 'b',
            'isCurrent': False, 'startDate': '2021', 'endDate': '2022'}
SCHOOL_ROW = {'company': 'SJSU', 'dateLabel': 'May 2018', 'title': 'BS', 'body': '.'}
LANG_ROW = {'ability': 'ReactJS', 'stars': '5'}


def put(client, updates):
    return client.put('/updateResume', json={'updates': updates}, headers=auth(client))


def test_a_whole_list_replaces_the_section(client, db):
    rows = [dict(WORK_ROW, company='Ambii'), dict(WORK_ROW, company='Google')]
    r = put(client, {'experiences.work': rows})
    assert r.status_code == 200
    stored = db['resumes'].docs[0]['experiences']['work']
    assert [w['company'] for w in stored] == ['Ambii', 'Google']


def test_every_list_path_is_writable(client, db):
    r = put(client, {
        'experiences.school': [SCHOOL_ROW],
        'experiences.work': [WORK_ROW],
        'abilities.languages': [LANG_ROW],
        'abilities.technologies': [dict(LANG_ROW, ability='Git')],
    })
    assert r.status_code == 200


def test_a_row_missing_the_unrendered_keys_is_refused(client, db):
    """The single most important test in this file.

    The UI renders only company/dateLabel/title/body. A client that echoes back
    what it renders would $set a work list without isCurrent/startDate/endDate,
    and $set REPLACES the array -- so those three would be deleted. They are
    exactly what sort_work_items orders by, so every key would collapse to
    (0, '', ''), the ordering would become a permanent no-op, and the damage
    would be invisible until the next row was added, because a stable sort
    leaves the just-written order alone.
    """
    before = copy.deepcopy(db['resumes'].docs[0]['experiences']['work'])
    rendered_only = {'company': 'A', 'dateLabel': 'x', 'title': 't', 'body': 'b'}

    r = put(client, {'experiences.work': [rendered_only]})

    assert r.status_code == 400
    assert r.get_json()['code'] == 'validation_failed'
    detail = r.get_json()['errors'][0]['detail']
    assert 'missing' in detail
    for key in ('endDate', 'isCurrent', 'startDate'):
        assert key in detail
    # And nothing was written.
    assert db['resumes'].docs[0]['experiences']['work'] == before


def test_a_mongo_operator_row_is_refused(client, db):
    """isinstance(value, str) used to be the whole defence against an operator
    document reaching $set. A list value cannot use it, so the row schema has
    to rebuild it -- an operator key is simply not in the expected key set."""
    r = put(client, {'abilities.languages': [{'$ne': None}]})
    assert r.status_code == 400
    assert 'unexpected $ne' in r.get_json()['errors'][0]['detail']


def test_a_mongo_operator_as_a_value_is_refused(client, db):
    r = put(client, {'abilities.languages': [{'ability': {'$gt': ''}, 'stars': '5'}]})
    assert r.status_code == 400
    assert 'must be a string, got dict' in r.get_json()['errors'][0]['detail']


def test_an_empty_list_cannot_wipe_a_section(client, db):
    before = copy.deepcopy(db['resumes'].docs[0]['abilities']['languages'])
    r = put(client, {'abilities.languages': []})
    assert r.status_code == 400
    assert db['resumes'].docs[0]['abilities']['languages'] == before


def test_unknown_keys_in_a_row_are_refused(client, db):
    r = put(client, {'abilities.languages': [dict(LANG_ROW, sneaky='1')]})
    assert r.status_code == 400
    assert 'unexpected sneaky' in r.get_json()['errors'][0]['detail']


def test_a_dotted_key_in_a_row_is_refused(client, db):
    """A dotted key would be interpreted as a path by some drivers."""
    r = put(client, {'abilities.languages': [dict(LANG_ROW, **{'a.b': '1'})]})
    assert r.status_code == 400
    assert 'unexpected a.b' in r.get_json()['errors'][0]['detail']


@pytest.mark.parametrize('value', ['nope', 42, None, {'0': LANG_ROW}])
def test_a_list_path_requires_an_actual_list(client, db, value):
    r = put(client, {'abilities.languages': value})
    assert r.status_code == 400
    assert 'must be a list' in r.get_json()['errors'][0]['detail']


def test_too_many_rows_are_refused(client, db):
    r = put(client, {'abilities.languages': [LANG_ROW] * (server.MAX_ROWS + 1)})
    assert r.status_code == 400
    assert f'more than {server.MAX_ROWS} rows' in r.get_json()['errors'][0]['detail']


@pytest.mark.parametrize('stars', ['9', '-1', '', 'five', '3.0'])
def test_stars_outside_zero_to_five_are_refused(client, db, stars):
    r = put(client, {'abilities.languages': [dict(LANG_ROW, stars=stars)]})
    assert r.status_code == 400
    assert 'stars must be one of' in r.get_json()['errors'][0]['detail']


@pytest.mark.parametrize('stars', ['0', '1', '2', '3', '4', '5'])
def test_every_valid_star_count_is_accepted(client, db, stars):
    r = put(client, {'abilities.languages': [dict(LANG_ROW, stars=stars)]})
    assert r.status_code == 200


def test_stars_must_be_a_string_not_a_number(client, db):
    """The document stores star counts as strings ('5'), and normalizeStars on
    the client coerces. Accepting an int here would let the two representations
    diverge in one collection."""
    r = put(client, {'abilities.languages': [dict(LANG_ROW, stars=5)]})
    assert r.status_code == 400
    assert 'stars must be a string, got int' in r.get_json()['errors'][0]['detail']


def test_isCurrent_must_be_a_real_bool(client, db):
    """isinstance(1, bool) is False, and that is wanted: sort_work_items
    branches on truthiness, so a stray 1 would work right up until someone
    stored the string '0', which is truthy."""
    r = put(client, {'experiences.work': [dict(WORK_ROW, isCurrent=1)]})
    assert r.status_code == 400
    assert 'isCurrent must be true or false, got int' in r.get_json()['errors'][0]['detail']


def test_a_bool_cannot_pass_as_a_string_field(client, db):
    r = put(client, {'abilities.languages': [dict(LANG_ROW, ability=True)]})
    assert r.status_code == 400
    assert 'ability must be a string, got bool' in r.get_json()['errors'][0]['detail']


def test_a_row_value_longer_than_the_cap_is_refused(client, db):
    r = put(client, {'experiences.school': [dict(SCHOOL_ROW, body='z' * 4001)]})
    assert r.status_code == 400
    assert 'longer than' in r.get_json()['errors'][0]['detail']


def test_errors_name_the_row_but_keep_the_path_the_ui_knows(client, db):
    """src/utils/adminApi.js keys field errors by the same dotted path the
    drafts are keyed by. An error reported against 'abilities.languages.2.stars'
    could never match anything the UI holds, so the index goes in `detail`."""
    rows = [LANG_ROW, LANG_ROW, dict(LANG_ROW, stars='9')]
    r = put(client, {'abilities.languages': rows})
    assert r.status_code == 400
    error = r.get_json()['errors'][0]
    assert error['path'] == 'abilities.languages'
    assert error['detail'].startswith('row 2:')


def test_the_error_list_is_capped(client, db):
    rows = [dict(LANG_ROW, stars='9')] * 40
    r = put(client, {'abilities.languages': rows})
    assert r.status_code == 400
    assert len(r.get_json()['errors']) <= server.MAX_ERRORS


def test_one_bad_row_rejects_the_whole_batch(client, db):
    before = copy.deepcopy(db['resumes'].docs[0])
    r = put(client, {'profile.name': 'Ada',
                     'abilities.languages': [dict(LANG_ROW, stars='9')]})
    assert r.status_code == 400
    assert db['resumes'].docs[0] == before


def test_the_stored_row_is_rebuilt_not_the_callers_object(client, db):
    """Values are copied into fresh dicts keyed only by the schema, so nothing
    the caller sent reaches the driver by reference."""
    sent = dict(LANG_ROW)
    r = put(client, {'abilities.languages': [sent]})
    assert r.status_code == 200
    stored = db['resumes'].docs[0]['abilities']['languages'][0]
    assert stored == LANG_ROW
    assert stored is not sent


def test_a_list_write_is_backed_up_like_any_other(client, db):
    put(client, {'abilities.languages': [LANG_ROW]})
    record = db['backups'].inserted[-1]
    assert record['changed_paths'] == ['abilities.languages']
    assert record['actor'] == 'austin'
    assert record['previous']['abilities']['languages'] == [{'ability': 'ReactJS', 'stars': '5'}]


def test_index_addressed_array_paths_remain_unwritable(client, db):
    """The whole point of writing lists whole. These must stay refused."""
    for path in ('experiences.work.0.title', 'abilities.languages.0.stars',
                 'experiences.school.0.company'):
        r = put(client, {path: 'x'})
        assert r.status_code == 400, path
        assert r.get_json()['errors'][0]['detail'] == 'not a writable field'


def test_writing_back_the_served_work_order_is_idempotent(client, db):
    """The client sends the list back in the order it was served, and that
    order is sort_work_items' own output -- so a save must not reshuffle it."""
    served = client.get('/getResume').get_json()['experiences']['work']
    r = put(client, {'experiences.work': served})
    assert r.status_code == 200
    assert r.get_json()['experiences']['work'] == served


# ===========================================================================
# GET /version
#
# The endpoint exists because a deploy was unverifiable, so its tests are about
# the two properties that make it usable at all: no credential, no database.
# ===========================================================================

def test_version_needs_no_token(client):
    # The whole point. Needing a credential to check a deploy is what made the
    # last one go unchecked -- and every other route here either returns the
    # same document on any build or answers 401.
    r = client.get('/version')
    assert r.status_code == 200
    assert set(r.get_json()) == {'sha', 'short'}


def test_version_answers_while_the_database_is_dead(client, monkeypatch):
    # It has to distinguish a bad DEPLOY from a bad DATABASE, so it must not
    # touch Atlas. Note this test deliberately does NOT use the `db` fixture:
    # nothing is stubbed, so any collection access would raise.
    def boom():
        raise AssertionError('/version touched the database')

    monkeypatch.setattr(server, 'resumes', boom)
    monkeypatch.setattr(server, 'admins', boom)
    monkeypatch.setattr(server, 'backups', boom)
    monkeypatch.setattr(server, 'get_client', boom)

    r = client.get('/version')
    assert r.status_code == 200


def test_version_reports_the_baked_in_sha(client, monkeypatch):
    monkeypatch.setattr(server, 'GIT_SHA', 'b9e8837fdac98733e9600dfdf7a071808bb137eb')
    body = client.get('/version').get_json()
    assert body['sha'] == 'b9e8837fdac98733e9600dfdf7a071808bb137eb'
    # Short form so a human can compare it to `git log --oneline` at a glance.
    assert body['short'] == 'b9e8837'


def test_version_says_unknown_rather_than_guessing(client, monkeypatch):
    # A local `flask run` has no ARG baked in. Reporting an empty string, or the
    # string "None", would look like a real answer in a deploy check.
    monkeypatch.setattr(server, 'GIT_SHA', 'unknown')
    assert client.get('/version').get_json() == {'sha': 'unknown', 'short': 'unknown'}


def test_version_refuses_writes(client):
    # It sits under the same /api/ proxy as everything else. The public vhost's
    # limit_except stops non-GET at the edge, but the route must not accept them
    # either -- the admin vhost carries no such restriction.
    for method in ('post', 'put', 'delete', 'patch'):
        assert getattr(client, method)('/version').status_code == 405


def test_git_sha_is_read_from_the_environment():
    """The wiring, not just the route.

    Every test above monkeypatches server.GIT_SHA, so all of them would still
    pass if the Dockerfile's ENV name and the os.getenv call disagreed -- and
    the endpoint would report "unknown" for ever while looking healthy. That is
    precisely the silent-uselessness this endpoint exists to prevent, so it gets
    its own check.

    A subprocess rather than importlib.reload: reloading server rebuilds the
    Flask app other tests hold a reference to.
    """
    import subprocess
    import sys

    script = (
        'import os;'
        'os.environ["GIT_SHA"]="deadbeefcafe";'
        'os.environ.setdefault("ADMIN_SESSION_SECRET","k"*40);'
        'os.environ.setdefault("DBUSER","u");'
        'os.environ.setdefault("DBPASS","p");'
        'import server;'
        'print(server.GIT_SHA)'
    )
    out = subprocess.run(
        [sys.executable, '-c', script],
        capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == 'deadbeefcafe'
