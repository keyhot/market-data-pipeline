from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_chart_page_serves_html_with_symbol():
    response = client.get("/chart/aapl")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "AAPL" in response.text
    assert "/bars/AAPL" in response.text
    assert "__SYMBOL__" not in response.text


def test_chart_page_rejects_injection_attempts():
    assert client.get("/chart/%3Cscript%3E").status_code == 400
    assert client.get("/chart/AAPL%22%3E").status_code == 400


def test_chart_page_rejects_overlong_symbol():
    assert client.get("/chart/" + "A" * 16).status_code == 400
