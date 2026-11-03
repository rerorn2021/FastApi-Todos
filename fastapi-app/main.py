import json
import os

from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from prometheus_client.core import REGISTRY, GaugeMetricFamily
from prometheus_client.registry import Collector
from prometheus_fastapi_instrumentator import Instrumentator  # ← 추가
from pydantic import BaseModel, Field, StringConstraints, ValidationError

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

Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


Priority = Literal["low", "medium", "high"]
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}  # 정렬 순서 — 높음이 먼저
SOON = timedelta(days=3)                            # 화면의 '임박' 기준과 같다


class TodoIn(BaseModel):                         # 클라이언트가 보내는 데이터 (id 없음)
    title: str = Field(min_length=1, max_length=100)
    description: str = ""
    completed: bool = False
    due_date: datetime | None = None              # 마감일 (분 단위, 선택)
    category_id: int | None = None                # 분류 (없으면 미분류)
    hidden: bool = False                          # 숨김 — 목록에서 빼두고 숨김 탭에서만 본다
    priority: Priority = "medium"                 # 우선순위


class TodoPatch(BaseModel):                       # 부분 수정 — 보낸 필드만 바꾼다
    title: str | None = None
    description: str | None = None
    completed: bool | None = None
    due_date: datetime | None = None
    category_id: int | None = None
    hidden: bool | None = None
    priority: Priority | None = None


class TodoItem(TodoIn):                          # 서버가 돌려주는 데이터 (id 있음)
    id: int
    created_at: datetime | None = None            # 서버가 기록 — 이 기능 전에 만든 항목은 없음
    updated_at: datetime | None = None


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


def local_naive(d: datetime) -> datetime:        # 시간대가 붙어 와도 서버 현지 시각으로 맞춰 비교
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d


def due_status(todo: TodoItem, now: datetime) -> str | None:  # 미완료 항목의 마감 상태
    if todo.due_date is None or todo.completed:
        return None
    due = local_naive(todo.due_date)
    if due < now:
        return "overdue"
    if due.date() == now.date():
        return "today"
    if due <= now + SOON:
        return "upcoming"
    return None


SORT_KEYS = {                                     # 마감일·생성일이 없는 항목은 맨 뒤로
    "due_date": lambda t: (t.due_date is None, t.due_date and local_naive(t.due_date)),
    "priority": lambda t: PRIORITY_RANK[t.priority],
    "created_at": lambda t: (t.created_at is None, t.created_at and local_naive(t.created_at)),
    "title": lambda t: t.title.casefold(),
}


@app.get("/todos")                               # 목록 조회 — 검색(q), 마감 상태(status), 정렬(sort)
def get_todos(
    q: str = "",
    status: Literal["overdue", "today", "upcoming"] | None = None,
    sort: Literal["due_date", "priority", "created_at", "title"] | None = None,
) -> list[TodoItem]:
    keyword = q.strip().casefold()
    now = datetime.now()
    todos = [t for t in load_todos() if keyword in f"{t.title}\n{t.description}".casefold()]
    if status:
        todos = [t for t in todos if due_status(t, now) == status]
    return sorted(todos, key=SORT_KEYS[sort]) if sort else todos


@app.post("/todos", status_code=201)             # 추가 — id 는 서버가 매긴다
def create_todo(payload: TodoIn) -> TodoItem:
    check_category(payload.category_id)
    todos = load_todos()
    new_id = max((t.id for t in todos), default=0) + 1
    now = datetime.now()
    todo = TodoItem(id=new_id, created_at=now, updated_at=now, **payload.model_dump())
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
    index = find_index(todos, todo_id)
    todo = TodoItem(id=todo_id, created_at=todos[index].created_at, updated_at=datetime.now(), **payload.model_dump())
    todos[index] = todo
    save_todos(todos)
    return todo


@app.patch("/todos/{todo_id}")                   # 부분 수정 — 완료 체크·숨김처럼 일부 필드만 바꿀 때
def patch_todo(todo_id: int, payload: TodoPatch) -> TodoItem:
    todos = load_todos()
    index = find_index(todos, todo_id)
    changes = payload.model_dump(exclude_unset=True)
    if "category_id" in changes:
        check_category(changes["category_id"])
    try:                                          # 합친 결과를 TodoIn 규칙(제목 길이 등)으로 다시 검사
        merged = TodoIn(**{**todos[index].model_dump(include=set(TodoIn.model_fields)), **changes})
    except ValidationError as e:
        raise RequestValidationError(e.errors()) from e
    todo = TodoItem(id=todo_id, created_at=todos[index].created_at, updated_at=datetime.now(), **merged.model_dump())
    todos[index] = todo
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


class TodoCollector(Collector):                   # /metrics 스크레이프 때마다 파일을 읽어 할 일 현황을 내보낸다
    def collect(self):
        todos = load_todos()
        now = datetime.now()
        items = GaugeMetricFamily("todo_items", "Number of to-do items by state", labels=["state"])
        items.add_metric(["total"], len(todos))
        items.add_metric(["completed"], sum(t.completed for t in todos))
        items.add_metric(["pending"], sum(not t.completed for t in todos))
        items.add_metric(["hidden"], sum(t.hidden for t in todos))
        items.add_metric(["overdue"], sum(due_status(t, now) == "overdue" for t in todos))
        yield items
        pending = GaugeMetricFamily("todo_pending_by_priority", "Pending to-do items by priority", labels=["priority"])
        for priority in PRIORITY_RANK:
            pending.add_metric([priority], sum(not t.completed and t.priority == priority for t in todos))
        yield pending


REGISTRY.register(TodoCollector())


@app.get("/", include_in_schema=False)           # 화면 서빙
def read_root() -> FileResponse:
    return FileResponse(INDEX_FILE, media_type="text/html")