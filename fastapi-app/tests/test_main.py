import pytest
from fastapi.testclient import TestClient

import main
from main import app, save_todos, load_todos, TodoItem, save_categories, load_categories, Category

client = TestClient(app)

@pytest.fixture(autouse=True)
def setup_and_teardown(tmp_path, monkeypatch):
    # 실제 todo.json 대신 테스트마다 새 임시 파일 사용 (본인 데이터 보호 + 테스트 간 격리)
    # TODO_FILE 은 본인 main.py의 파일 경로 변수명에 맞게 수정 (Path 객체 그대로 넘김, str()로 감싸지 않음)
    monkeypatch.setattr(main, "TODO_FILE", tmp_path / "todo.json")
    monkeypatch.setattr(main, "CATEGORY_FILE", tmp_path / "categories.json")  # 분류 파일도 격리
    save_todos([])  # 테스트 전 초기화
    save_categories([])
    yield
    # 테스트 후 정리: tmp_path 와 monkeypatch 가 자동으로 원상 복구

def test_get_todos_empty():
    response = client.get("/todos")
    assert response.status_code == 200
    assert response.json() == []

def test_get_todos_with_items():
    todo = TodoItem(id=1, title="Test", description="Test description", completed=False)
    save_todos([todo])  # save_todos 는 TodoItem 객체 리스트를 받음
    response = client.get("/todos")
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["title"] == "Test"

def test_create_todo():
    todo = {"title": "Test", "description": "Test description", "completed": False}  # id 는 보내지 않음
    response = client.post("/todos", json=todo)
    assert response.status_code == 201           # 생성 성공 = 201 Created
    assert response.json()["title"] == "Test"
    assert response.json()["id"] == 1            # id 는 서버가 부여
    assert len(load_todos()) == 1                # 파일에도 저장됐는지 확인

def test_create_todo_invalid():
    todo = {"description": "Test description"}   # 필수 필드 title 누락
    response = client.post("/todos", json=todo)
    assert response.status_code == 422

def test_update_todo():
    todo = TodoItem(id=1, title="Test", description="Test description", completed=False)
    save_todos([todo])
    updated_todo = {"title": "Updated", "description": "Updated description", "completed": True}
    response = client.put("/todos/1", json=updated_todo)
    assert response.status_code == 200
    assert response.json()["title"] == "Updated"

def test_update_todo_not_found():
    updated_todo = {"title": "Updated", "description": "Updated description", "completed": True}
    response = client.put("/todos/1", json=updated_todo)
    assert response.status_code == 404

def test_delete_todo():
    todo = TodoItem(id=1, title="Test", description="Test description", completed=False)
    save_todos([todo])
    response = client.delete("/todos/1")
    assert response.status_code == 204           # 삭제 성공 = 204 No Content (응답 본문 없음)
    assert load_todos() == []

def test_delete_todo_not_found():
    response = client.delete("/todos/1")
    assert response.status_code == 404

def test_search_todos():
    save_todos([
        TodoItem(id=1, title="장보기", description="Milk"),
        TodoItem(id=2, title="과제", description="DevOps 보고서"),
    ])
    assert [t["id"] for t in client.get("/todos", params={"q": "milk"}).json()] == [1]  # 대소문자 무시
    assert [t["id"] for t in client.get("/todos", params={"q": "보고서"}).json()] == [2]  # 설명에서도 검색
    assert len(client.get("/todos", params={"q": "  "}).json()) == 2                     # 공백만 = 전체

def test_hide_todo():
    save_todos([TodoItem(id=1, title="Test")])
    response = client.put("/todos/1", json={"title": "Test", "hidden": True})
    assert response.status_code == 200
    assert load_todos()[0].hidden is True

def test_todo_not_hidden_by_default():
    response = client.post("/todos", json={"title": "Test"})
    assert response.json()["hidden"] is False

def test_delete_completed_todos():
    save_todos([
        TodoItem(id=1, title="done", completed=True),
        TodoItem(id=2, title="todo", completed=False),
        TodoItem(id=3, title="hidden done", completed=True, hidden=True),
    ])
    response = client.delete("/todos/completed")
    assert response.status_code == 200
    assert response.json() == {"deleted": 1}
    assert [t.id for t in load_todos()] == [2, 3]  # 숨긴 완료 항목은 남는다

def test_delete_completed_hidden_todos():
    save_todos([
        TodoItem(id=1, title="done", completed=True),
        TodoItem(id=2, title="hidden done", completed=True, hidden=True),
    ])
    response = client.delete("/todos/completed", params={"hidden": True})
    assert response.json() == {"deleted": 1}
    assert [t.id for t in load_todos()] == [1]


# ---- 할 일: 입력값 경계 ----

@pytest.mark.parametrize("title, status", [("", 422), ("a" * 100, 201), ("a" * 101, 422)])
def test_create_todo_title_length(title, status):
    assert client.post("/todos", json={"title": title}).status_code == status

def test_create_todo_with_due_date():
    response = client.post("/todos", json={"title": "Test", "due_date": "2026-10-10T18:30"})
    assert response.status_code == 201
    assert response.json()["due_date"] == "2026-10-10T18:30:00"   # 저장한 값이 그대로 돌아옴

def test_create_todo_invalid_due_date():
    assert client.post("/todos", json={"title": "Test", "due_date": "내일"}).status_code == 422

def test_create_todo_id_after_delete():
    save_todos([TodoItem(id=1, title="a"), TodoItem(id=3, title="b")])  # 중간(2)이 지워진 상태
    assert client.post("/todos", json={"title": "c"}).json()["id"] == 4  # 가장 큰 id + 1

def test_create_todo_unknown_category():
    assert client.post("/todos", json={"title": "Test", "category_id": 99}).status_code == 400

def test_update_todo_unknown_category():
    save_todos([TodoItem(id=1, title="Test")])
    assert client.put("/todos/1", json={"title": "Test", "category_id": 99}).status_code == 400


# ---- 분류 ----

def test_get_categories():
    save_categories([Category(id=1, name="학교")])
    assert client.get("/categories").json() == [{"id": 1, "name": "학교"}]

def test_create_category():
    response = client.post("/categories", json={"name": "  학교  "})
    assert response.status_code == 201
    assert response.json() == {"id": 1, "name": "학교"}            # 앞뒤 공백은 잘림
    assert load_categories() == [Category(id=1, name="학교")]

@pytest.mark.parametrize("name", ["", "   ", "a" * 31])
def test_create_category_invalid_name(name):
    assert client.post("/categories", json={"name": name}).status_code == 422

def test_create_category_duplicate():
    save_categories([Category(id=1, name="학교")])
    assert client.post("/categories", json={"name": " 학교"}).status_code == 409  # 공백 제거 후 비교

def test_todo_with_category():
    save_categories([Category(id=1, name="학교")])
    response = client.post("/todos", json={"title": "과제", "category_id": 1})
    assert response.status_code == 201
    assert response.json()["category_id"] == 1

def test_update_category():
    save_categories([Category(id=1, name="학교")])
    response = client.put("/categories/1", json={"name": "학업"})
    assert response.status_code == 200
    assert load_categories() == [Category(id=1, name="학업")]

def test_update_category_same_name():
    save_categories([Category(id=1, name="학교")])
    assert client.put("/categories/1", json={"name": "학교"}).status_code == 200  # 자기 자신과는 중복 아님

def test_update_category_duplicate():
    save_categories([Category(id=1, name="학교"), Category(id=2, name="집")])
    assert client.put("/categories/2", json={"name": "학교"}).status_code == 409

def test_update_category_not_found():
    assert client.put("/categories/1", json={"name": "학교"}).status_code == 404

def test_delete_category_moves_todos_to_uncategorized():
    save_categories([Category(id=1, name="학교"), Category(id=2, name="집")])
    save_todos([TodoItem(id=1, title="과제", category_id=1), TodoItem(id=2, title="청소", category_id=2)])
    assert client.delete("/categories/1").status_code == 204
    assert load_categories() == [Category(id=2, name="집")]
    assert [t.category_id for t in load_todos()] == [None, 2]   # 다른 분류의 할 일은 그대로

def test_delete_category_not_found():
    assert client.delete("/categories/1").status_code == 404


# ---- 화면 ----

def test_index_page():
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


# ---- 데이터 폴더 (DATA_DIR) ----

def test_data_dir_from_env(tmp_path):
    # 모듈 설정은 import 시점에 정해지므로, 깨끗한 새 프로세스에서 import 해서 확인
    import os, subprocess, sys
    data_dir = tmp_path / "data"                                    # 아직 없는 폴더
    code = "import main; print(main.TODO_FILE); print(main.CATEGORY_FILE)"
    result = subprocess.run([sys.executable, "-c", code], cwd=main.BASE_DIR, capture_output=True, text=True,
                            env={**os.environ, "DATA_DIR": str(data_dir)}, check=True)
    assert result.stdout.split() == [str(data_dir / "todo.json"), str(data_dir / "categories.json")]
    assert (data_dir / "todo.json").read_text(encoding="utf-8") == "[]"  # 폴더와 빈 파일을 만들어 둠
    assert (data_dir / "categories.json").read_text(encoding="utf-8") == "[]"
