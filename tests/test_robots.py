from fastapi.testclient import TestClient

from sous.api import app


def test_robots_txt_disallows_all():
    r = TestClient(app).get("/robots.txt")
    assert r.status_code == 200
    assert "Disallow: /" in r.text
