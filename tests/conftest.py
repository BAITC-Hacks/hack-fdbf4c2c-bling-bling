import os
os.environ['DATABASE_URL'] = 'sqlite:////tmp/hackalem-tests.sqlite'
os.environ['DATA_DIR'] = '/tmp/hackalem-tests-data'
os.environ.setdefault('SERVICE_TOKEN', 'test-service-token')
os.environ.setdefault('WEBHOOK_TOKEN', 'test-webhook-token')
os.environ.setdefault('APP_SECRET', 'test-secret')
os.environ.setdefault('ADMIN_PASSWORD', 'test-password')
import pytest
from backend.app.db import Base, engine


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
