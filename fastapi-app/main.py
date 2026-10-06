import json
import os
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StringConstraints

BASE_DIR = Path(__file__).resolve().parent       # main.py 가 있는 폴더
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))  # 데이터 폴더 — 컨테이너에선 볼륨 경로로 지정
DATA_DIR.mkdir(parents=True, exist_ok=True)
TODO_FILE = DATA_DIR / "todo.json"
CATEGORY_FILE = DATA_DIR / "categories.json"
INDEX_FILE = BASE_DIR / "templates" / "index.html"

for f in (TODO_FILE, CATEGORY_FILE):             # 없으면 빈 목록으로 만들어 둔다
    if not f.exists():
        f.write_text("[]", encoding="utf-8")

app = FastAPI(title="To-Do List API")


class TodoIn(BaseModel):                         # 클라이언트가 보내는 데이터 (id 없음)
    title: str = Field(min_length=1, max_length=100)
    description: str = ""
    completed: bool = False
    due_date: datetime | None = None              # 마감일 (분 단위, 선택)
    category_id: int | None = None                # 분류 (없으면 미분류)
    hidden: bool = False                          # 숨김 — 목록에서 빼두고 숨김 탭에서만 본다


class TodoItem(TodoIn):                          # 서버가 돌려주는 데이터 (id 있음)
    id: int


class CategoryIn(BaseModel):                      # 분류 (대분류 한 단계)
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30)]


class Category(CategoryIn):
    id: int


def load_json(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8") if path.exists() else "[]")


def save_json(path: Path, items: list[BaseModel]) -> None:
    data = json.dumps([i.model_dump(mode="json") for i in items], indent=2, ensure_ascii=False)
    path.write_text(data, encoding="utf-8")


def load_todos() -> list[TodoItem]:
    return [TodoItem(**t) for t in load_json(TODO_FILE)]


def save_todos(todos: list[TodoItem]) -> None:
    save_json(TODO_FILE, todos)


def load_categories() -> list[Category]:
    return [Category(**c) for c in load_json(CATEGORY_FILE)]


def save_categories(categories: list[Category]) -> None:
    save_json(CATEGORY_FILE, categories)


def find_index(items: list[TodoItem] | list[Category], item_id: int, what: str = "To-Do item") -> int:
    for i, item in enumerate(items):
        if item.id == item_id:
            return i
    raise HTTPException(404, f"{what} not found")


def check_category(category_id: int | None) -> None:
    if category_id is not None and all(c.id != category_id for c in load_categories()):
        raise HTTPException(400, "Category not found")


def check_unique_name(categories: list[Category], name: str, exclude_id: int | None = None) -> None:
    if any(c.name == name and c.id != exclude_id for c in categories):
        raise HTTPException(409, "Category name already exists")


@app.get("/todos")                               # 목록 조회 — q 가 있으면 제목·설명에서 검색
def get_todos(q: str = "") -> list[TodoItem]:
    keyword = q.strip().casefold()
    return [t for t in load_todos() if keyword in f"{t.title}\n{t.description}".casefold()]


@app.post("/todos", status_code=201)             # 추가 — id 는 서버가 매긴다
def create_todo(payload: TodoIn) -> TodoItem:
    check_category(payload.category_id)
    todos = load_todos()
    new_id = max((t.id for t in todos), default=0) + 1
    todo = TodoItem(id=new_id, **payload.model_dump())
    save_todos(todos + [todo])
    return todo


@app.delete("/todos/completed")                  # 완료 항목 일괄 삭제 — 숨김 여부가 같은 것만 (/todos/{todo_id} 보다 먼저 선언)
def delete_completed_todos(hidden: bool = False) -> dict[str, int]:
    todos = load_todos()
    remaining = [t for t in todos if not (t.completed and t.hidden == hidden)]
    save_todos(remaining)
    return {"deleted": len(todos) - len(remaining)}


@app.put("/todos/{todo_id}")                     # 수정
def update_todo(todo_id: int, payload: TodoIn) -> TodoItem:
    check_category(payload.category_id)
    todos = load_todos()
    todo = TodoItem(id=todo_id, **payload.model_dump())
    todos[find_index(todos, todo_id)] = todo
    save_todos(todos)
    return todo


@app.delete("/todos/{todo_id}", status_code=204)  # 삭제
def delete_todo(todo_id: int) -> None:
    todos = load_todos()
    del todos[find_index(todos, todo_id)]
    save_todos(todos)


@app.get("/categories")                          # 분류 목록
def get_categories() -> list[Category]:
    return load_categories()


@app.post("/categories", status_code=201)        # 분류 추가
def create_category(payload: CategoryIn) -> Category:
    categories = load_categories()
    name = payload.name
    check_unique_name(categories, name)
    category = Category(id=max((c.id for c in categories), default=0) + 1, name=name)
    save_categories(categories + [category])
    return category


@app.put("/categories/{category_id}")            # 분류 이름 변경
def update_category(category_id: int, payload: CategoryIn) -> Category:
    categories = load_categories()
    index = find_index(categories, category_id, "Category")
    name = payload.name
    check_unique_name(categories, name, exclude_id=category_id)
    categories[index] = Category(id=category_id, name=name)
    save_categories(categories)
    return categories[index]


@app.delete("/categories/{category_id}", status_code=204)  # 분류 삭제 — 속한 할 일은 미분류로 옮긴다
def delete_category(category_id: int) -> None:
    categories = load_categories()
    del categories[find_index(categories, category_id, "Category")]
    save_categories(categories)
    todos = load_todos()
    for todo in todos:
        if todo.category_id == category_id:
            todo.category_id = None
    save_todos(todos)


@app.get("/", include_in_schema=False)           # 화면 서빙
def read_root() -> FileResponse:
    return FileResponse(INDEX_FILE, media_type="text/html")