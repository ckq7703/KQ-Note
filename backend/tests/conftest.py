import os
import sys
import tempfile
import uuid

# Never let a test touch a real database: force the URL before the app is imported.
# Set TEST_DATABASE_URL (a disposable Postgres) to also run the concurrency tests.
_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
os.environ["DATABASE_URL"] = _TEST_DB_URL or f"sqlite:///{tempfile.mkdtemp()}/test.db"
os.environ["JWT_SECRET_KEY"] = "test-secret-key-not-for-production"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402

IS_POSTGRES = engine.dialect.name == "postgresql"


@pytest.fixture(autouse=True)
def fresh_db():
    from app.guard import limiter
    limiter.reset()  # tests register many users from one address; don't let one test spend another's budget
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _register(client, email):
    r = client.post("/auth/register", json={"email": email, "password": "password-123"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def alice(client):
    return _register(client, "alice@example.com")


@pytest.fixture
def bob(client):
    return _register(client, "bob@example.com")


def new_id():
    return str(uuid.uuid4())


class Api:
    """Thin helper so tests read like the client protocol."""

    def __init__(self, client, headers):
        self.c, self.h = client, headers

    def put(self, nid, content, base_rev=0, **kw):
        return self.c.put(f"/v2/notes/{nid}", headers=self.h,
                          json={"content": content, "base_rev": base_rev, **kw})

    def create(self, content="hello", **kw):
        nid = new_id()
        r = self.put(nid, content, 0, **kw)
        assert r.status_code == 200, r.text
        return nid, r.json()

    def get(self, nid):
        return self.c.get(f"/v2/notes/{nid}", headers=self.h)

    def changes(self, cursor=0, limit=200):
        return self.c.get(f"/v2/notes/changes?cursor={cursor}&limit={limit}", headers=self.h)

    def trash(self, nid, base_rev, **kw):
        return self.c.post(f"/v2/notes/{nid}/trash", headers=self.h, json={"base_rev": base_rev, **kw})

    def restore(self, nid, base_rev, **kw):
        return self.c.post(f"/v2/notes/{nid}/restore", headers=self.h, json={"base_rev": base_rev, **kw})

    def move(self, nid, position):
        return self.c.patch(f"/v2/notes/{nid}", headers=self.h, json={"position": position})

    def revisions(self, nid):
        return self.c.get(f"/v2/notes/{nid}/revisions", headers=self.h)


@pytest.fixture
def a(client, alice):
    return Api(client, alice)


@pytest.fixture
def b(client, bob):
    return Api(client, bob)
