# 배포된 서버에 실제 HTTP 요청을 보내 API를 확인하는 통합 테스트
# 실행: BASE_URL=http://[팀 서버 IP]:[port num] pytest integration_tests
import os
import time
import uuid

import httpx2
import pytest

BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
if not BASE_URL:  # 로컬에서 실수로 실행했을 때 에러 대신 건너뜀
    pytest.skip("BASE_URL 환경변수가 없어 통합 테스트를 건너뜀", allow_module_level=True)


def unique_name():
    # 배포 서버의 실제 데이터와 겹치지 않는 이름
    return f"통합테스트-{uuid.uuid4().hex[:8]}"


def wait_until_up(http, seconds=30):
    # 배포 직후에는 컨테이너가 아직 뜨는 중일 수 있으므로 응답이 올 때까지 기다림
    for _ in range(seconds):
        try:
            if http.get("/todos").status_code == 200:
                return
        except httpx2.TransportError:  # 연결 거부, 타임아웃 등
            pass
        time.sleep(1)
    pytest.fail(f"{BASE_URL} 에 접속할 수 없음 (컨테이너 실행 여부, 포트, 방화벽 확인)")


@pytest.fixture(scope="session")
def client():
    # 단위 테스트의 TestClient(app) 대신, 실제 배포 주소로 요청을 보내는 클라이언트
    with httpx2.Client(base_url=BASE_URL, timeout=5) as http:
        wait_until_up(http)
        yield http


@pytest.fixture
def todo(client):
    # 테스트용 항목을 하나 만들고, 테스트가 끝나면(실패해도) 지움 → 배포 서버의 실제 데이터는 그대로
    response = client.post("/todos", json={"title": unique_name(), "description": "integration test"})
    assert response.status_code == 201
    item = response.json()
    yield item
    client.delete(f"/todos/{item['id']}")  # 테스트 안에서 이미 지웠다면 404가 오지만 상관없음


@pytest.fixture
def category(client):
    # 테스트용 분류도 같은 방식으로 만들고 지움
    response = client.post("/categories", json={"name": unique_name()})
    assert response.status_code == 201
    item = response.json()
    yield item
    client.delete(f"/categories/{item['id']}")


def find_todo(client, todo_id):
    return next((t for t in client.get("/todos").json() if t["id"] == todo_id), None)


def test_index_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_create_and_get(client, todo):
    response = client.get("/todos")
    assert response.status_code == 200
    assert todo["id"] in [t["id"] for t in response.json()]


def test_update(client, todo):
    payload = {"title": todo["title"], "description": "updated", "completed": True}
    response = client.put(f"/todos/{todo['id']}", json=payload)
    assert response.status_code == 200
    assert response.json()["completed"] is True


def test_delete(client, todo):
    assert client.delete(f"/todos/{todo['id']}").status_code == 204
    assert client.delete(f"/todos/{todo['id']}").status_code == 404  # 이미 지운 항목은 404


def test_create_invalid(client):
    response = client.post("/todos", json={"description": "제목 없음"})  # 필수 필드 title 누락
    assert response.status_code == 422


def test_search(client, todo):
    response = client.get("/todos", params={"q": todo["title"]})
    assert response.status_code == 200
    assert [t["id"] for t in response.json()] == [todo["id"]]  # 고유한 제목이라 정확히 하나


def test_hide(client, todo):
    assert todo["hidden"] is False                              # 기본값은 보이는 상태
    payload = {"title": todo["title"], "hidden": True}
    assert client.put(f"/todos/{todo['id']}", json=payload).json()["hidden"] is True
    assert find_todo(client, todo["id"])["hidden"] is True      # 서버에 저장됐는지


def test_todo_with_category(client, todo, category):
    payload = {"title": todo["title"], "category_id": category["id"]}
    assert client.put(f"/todos/{todo['id']}", json=payload).json()["category_id"] == category["id"]


def test_todo_with_unknown_category(client, todo, category):
    client.delete(f"/categories/{category['id']}")              # 방금 지운 분류 = 존재하지 않는 분류
    payload = {"title": todo["title"], "category_id": category["id"]}
    assert client.put(f"/todos/{todo['id']}", json=payload).status_code == 400


def test_delete_category_moves_todo_to_uncategorized(client, todo, category):
    payload = {"title": todo["title"], "category_id": category["id"]}
    client.put(f"/todos/{todo['id']}", json=payload)
    assert client.delete(f"/categories/{category['id']}").status_code == 204
    assert find_todo(client, todo["id"])["category_id"] is None


def test_duplicate_category_name(client, category):
    response = client.post("/categories", json={"name": category["name"]})
    assert response.status_code == 409


# DELETE /todos/completed (완료 항목 일괄 삭제)는 일부러 테스트하지 않음:
# 배포 서버에서 실행하면 사용자의 실제 완료 항목까지 지워진다 → 단위 테스트(test_main.py)에서만 확인