import calendar
import csv
import functools
import io
import json
import os
import logging                                   # ← 추가
import sys                                       # ← 추가
import threading
import time                                      # ← 추가

from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Request  # ← Request 추가
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from prometheus_client.core import REGISTRY, GaugeMetricFamily
from prometheus_client.registry import Collector
from prometheus_fastapi_instrumentator import Instrumentator  # ← 추가
from pydantic import BaseModel, Field, StringConstraints, ValidationError, field_validator, model_validator

BASE_DIR = Path(__file__).resolve().parent       # main.py 가 있는 폴더
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))  # 데이터 폴더 — 컨테이너에선 볼륨 경로로 지정
DATA_DIR.mkdir(parents=True, exist_ok=True)
TODO_FILE = DATA_DIR / "todo.json"
CATEGORY_FILE = DATA_DIR / "categories.json"
TRASH_FILE = DATA_DIR / "trash.json"             # 휴지통 — 지운 할 일은 여기로 옮겨 두고 되돌릴 수 있다
INDEX_FILE = BASE_DIR / "templates" / "index.html"

for f in (TODO_FILE, CATEGORY_FILE, TRASH_FILE):  # 없으면 빈 목록으로 만들어 둔다
    if not f.exists():
        f.write_text("[]", encoding="utf-8")

app = FastAPI(title="To-Do List API")

Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

# ← 추가: 액세스 로그를 표준 출력(stdout)으로 기록
#   Docker가 stdout을 컨테이너 로그로 저장하고, Alloy가 그 로그를 읽어 Loki로 전송
custom_logger = logging.getLogger("custom.access")
custom_logger.setLevel(logging.INFO)
custom_logger.addHandler(logging.StreamHandler(sys.stdout))
custom_logger.propagate = False                  # 상위 로거로 넘겨 두 번 찍히는 것을 방지

# ← 추가: uvicorn 기본 액세스 로그 끄기 (아래 미들웨어가 대신 기록)
#   켜 두면 요청 1번에 로그가 2줄씩 남아 대시보드 카운트가 2배가 됨
logging.getLogger("uvicorn.access").disabled = True

@app.middleware("http")                          # ← 추가: 모든 요청을 한 줄씩 기록
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    duration = time.perf_counter() - start
    client = request.client.host if request.client else "-"
    # 형식: IP - "METHOD PATH HTTP/1.1" 상태코드 응답시간 → 7번 LogQL 쿼리가 이 형식으로 파싱
    custom_logger.info(
        f'{client} - "{request.method} {request.url.path} HTTP/1.1" '
        f'{response.status_code} {duration:.3f}s'
    )
    return response

Priority = Literal["low", "medium", "high"]
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}  # 정렬 순서 — 높음이 먼저
SOON = timedelta(days=3)                            # 화면의 '임박' 기준과 같다
Repeat = Literal["none", "daily", "weekly", "monthly"]
Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]


class Subtask(BaseModel):                         # 하위 작업 (체크리스트 한 줄)
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    done: bool = False


class TodoIn(BaseModel):                         # 클라이언트가 보내는 데이터 (id 없음)
    title: str = Field(min_length=1, max_length=100)
    description: str = ""
    completed: bool = False
    due_date: datetime | None = None              # 마감일 (분 단위, 선택)
    category_id: int | None = None                # 분류 (없으면 미분류)
    hidden: bool = False                          # 숨김 — 목록에서 빼두고 숨김 탭에서만 본다
    priority: Priority = "medium"                 # 우선순위
    tags: list[Tag] = Field(default_factory=list, max_length=10)          # 태그 — 분류와 달리 여러 개
    subtasks: list[Subtask] = Field(default_factory=list, max_length=50)  # 하위 작업
    repeat: Repeat = "none"                       # 반복 — 완료하면 다음 마감일로 새 항목을 만든다

    @field_validator("tags")
    @classmethod
    def unique_tags(cls, tags: list[str]) -> list[str]:  # 중복 태그는 하나만 (순서 유지)
        return list(dict.fromkeys(tags))

    @model_validator(mode="after")
    def repeat_needs_due_date(self):              # 반복은 '다음 마감일'을 계산해야 하므로 마감일이 필수
        if self.repeat != "none" and self.due_date is None:
            raise ValueError("repeat requires due_date")
        return self


class TodoPatch(BaseModel):                       # 부분 수정 — 보낸 필드만 바꾼다
    title: str | None = None
    description: str | None = None
    completed: bool | None = None
    due_date: datetime | None = None
    category_id: int | None = None
    hidden: bool | None = None
    priority: Priority | None = None
    tags: list[Tag] | None = None
    subtasks: list[Subtask] | None = None
    repeat: Repeat | None = None


class TodoItem(TodoIn):                          # 서버가 돌려주는 데이터 (id 있음)
    id: int
    created_at: datetime | None = None            # 서버가 기록 — 이 기능 전에 만든 항목은 없음
    updated_at: datetime | None = None


class TrashedTodo(TodoItem):                      # 휴지통 항목 — 지운 시각과 원래 자리를 기억한다
    deleted_at: datetime
    position: int = 0


class OrderIn(BaseModel):                         # 드래그로 바꾼 순서
    ids: list[int]


class IdsIn(BaseModel):
    ids: list[int] = Field(min_length=1)


class CategoryIn(BaseModel):                      # 분류 (대분류 한 단계)
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30)]


class Category(CategoryIn):
    id: int


class ImportIn(BaseModel):                        # /export 로 받은 JSON 그대로
    todos: list[TodoIn] = Field(default_factory=list, max_length=5000)
    categories: list[Category] = Field(default_factory=list, max_length=500)


# 여러 요청이 동시에 '읽고 → 고치고 → 쓰면' 한쪽 변경이 사라지거나 id 가 겹친다.
# 파일을 다루는 동안은 한 요청씩만 들어오게 잠근다 (RLock: 같은 스레드는 다시 잠가도 된다).
# 프로세스 하나(uvicorn 기본) 기준 — 워커를 여러 개 띄우면 파일 잠금이나 DB 가 필요하다.
LOCK = threading.RLock()


def locked(func):                                 # 엔드포인트 전체를 잠금 안에서 실행
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with LOCK:
            return func(*args, **kwargs)
    return wrapper


def load_json(path: Path) -> list[dict]:
    with LOCK:
        return json.loads(path.read_text(encoding="utf-8") if path.exists() else "[]")


def save_json(path: Path, items: list[BaseModel]) -> None:
    data = json.dumps([i.model_dump(mode="json") for i in items], indent=2, ensure_ascii=False)
    tmp = path.with_name(path.name + ".tmp")
    with LOCK:
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, path)                     # 통째로 바꿔치기 — 쓰다가 죽어도 파일이 반쯤 비지 않는다


def load_todos() -> list[TodoItem]:
    return [TodoItem(**t) for t in load_json(TODO_FILE)]


def save_todos(todos: list[TodoItem]) -> None:
    save_json(TODO_FILE, todos)


def load_categories() -> list[Category]:
    return [Category(**c) for c in load_json(CATEGORY_FILE)]


def save_categories(categories: list[Category]) -> None:
    save_json(CATEGORY_FILE, categories)


def load_trash() -> list[TrashedTodo]:
    return [TrashedTodo(**t) for t in load_json(TRASH_FILE)]


def save_trash(trash: list[TrashedTodo]) -> None:
    save_json(TRASH_FILE, trash)


def next_id(todos: list[TodoItem]) -> int:        # 휴지통 id 도 피해야 되돌릴 때 겹치지 않는다
    return max((t.id for t in [*todos, *load_trash()]), default=0) + 1


def move_to_trash(removed: list[tuple[int, TodoItem]]) -> None:  # (원래 자리, 항목)
    now = datetime.now()
    trashed = [TrashedTodo(**t.model_dump(), deleted_at=now, position=pos) for pos, t in removed]
    save_trash(load_trash() + trashed)


def find_index(items: list[TodoItem] | list[Category] | list[TrashedTodo], item_id: int, what: str = "To-Do item") -> int:
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


def add_month(d: datetime) -> datetime:           # 1/31 → 2/28 처럼 없는 날은 그 달 마지막 날로
    year, month = d.year + d.month // 12, d.month % 12 + 1
    return d.replace(year=year, month=month, day=min(d.day, calendar.monthrange(year, month)[1]))


REPEAT_STEP = {
    "daily": lambda d: d + timedelta(days=1),
    "weekly": lambda d: d + timedelta(weeks=1),
    "monthly": add_month,
}


def next_occurrence(todo: TodoItem, new_id: int, now: datetime) -> TodoItem:
    step = REPEAT_STEP[todo.repeat]
    due = step(todo.due_date)
    while local_naive(due) <= now:                # 밀린 반복은 건너뛰고 다음 미래 날짜로
        due = step(due)
    return TodoItem(**{
        **todo.model_dump(),
        "id": new_id, "completed": False, "due_date": due, "created_at": now, "updated_at": now,
        "subtasks": [{**s.model_dump(), "done": False} for s in todo.subtasks],
    })


def replace_todo(todos: list[TodoItem], index: int, todo: TodoItem) -> TodoItem:
    """수정 결과를 저장한다. 반복 할 일을 방금 완료했다면 다음 회차를 만들어 붙인다."""
    if todo.completed and not todos[index].completed and todo.repeat != "none":
        todos.append(next_occurrence(todo, next_id(todos), todo.updated_at))
        todo = todo.model_copy(update={"repeat": "none"})  # 반복은 새 항목이 이어받는다 (다시 체크해도 중복 생성 X)
    todos[index] = todo
    save_todos(todos)
    return todo


SORT_KEYS = {                                     # 마감일·생성일이 없는 항목은 맨 뒤로
    "due_date": lambda t: (t.due_date is None, t.due_date and local_naive(t.due_date)),
    "priority": lambda t: PRIORITY_RANK[t.priority],
    "created_at": lambda t: (t.created_at is None, t.created_at and local_naive(t.created_at)),
    "title": lambda t: t.title.casefold(),
}


@app.get("/todos")                               # 목록 조회 — 검색(q), 태그(tag), 마감 상태(status), 정렬(sort)
def get_todos(
    q: str = "",
    tag: str | None = None,
    status: Literal["overdue", "today", "upcoming"] | None = None,
    sort: Literal["due_date", "priority", "created_at", "title"] | None = None,
) -> list[TodoItem]:
    keyword = q.strip().casefold()
    now = datetime.now()
    todos = [t for t in load_todos() if keyword in "\n".join([t.title, t.description, *t.tags]).casefold()]
    if tag:
        todos = [t for t in todos if tag.strip() in t.tags]
    if status:
        todos = [t for t in todos if due_status(t, now) == status]
    return sorted(todos, key=SORT_KEYS[sort]) if sort else todos


@app.get("/tags")                                # 쓰고 있는 태그 목록 (가나다순)
def get_tags() -> list[str]:
    return sorted({tag for t in load_todos() for tag in t.tags}, key=str.casefold)


@app.post("/todos", status_code=201)             # 추가 — id 는 서버가 매긴다
@locked
def create_todo(payload: TodoIn) -> TodoItem:
    check_category(payload.category_id)
    todos = load_todos()
    now = datetime.now()
    todo = TodoItem(id=next_id(todos), created_at=now, updated_at=now, **payload.model_dump())
    save_todos(todos + [todo])
    return todo


@app.delete("/todos/completed")                  # 완료 항목 일괄 삭제(→ 휴지통) — 숨김 여부가 같은 것만 (/todos/{todo_id} 보다 먼저 선언)
@locked
def delete_completed_todos(hidden: bool = False) -> dict[str, int]:
    todos = load_todos()
    removed = [(i, t) for i, t in enumerate(todos) if t.completed and t.hidden == hidden]
    move_to_trash(removed)
    save_todos([t for t in todos if not (t.completed and t.hidden == hidden)])
    return {"deleted": len(removed)}


@app.put("/todos/order")                         # 드래그로 순서 바꾸기 (/todos/{todo_id} 보다 먼저 선언)
@locked
def reorder_todos(payload: OrderIn) -> list[TodoItem]:
    todos = load_todos()
    by_id = {t.id: t for t in todos}
    wanted = set(payload.ids)
    if len(wanted) != len(payload.ids) or not wanted <= by_id.keys():
        raise HTTPException(400, "Unknown or duplicated ids")
    # 보낸 id 들이 차지하던 자리 안에서만 섞는다 — 다른 탭·검색에 안 보이던 항목 자리는 그대로
    moved = iter([by_id[i] for i in payload.ids])
    todos = [next(moved) if t.id in wanted else t for t in todos]
    save_todos(todos)
    return todos


@app.put("/todos/{todo_id}")                     # 수정
@locked
def update_todo(todo_id: int, payload: TodoIn) -> TodoItem:
    check_category(payload.category_id)
    todos = load_todos()
    index = find_index(todos, todo_id)
    todo = TodoItem(id=todo_id, created_at=todos[index].created_at, updated_at=datetime.now(), **payload.model_dump())
    return replace_todo(todos, index, todo)


@app.patch("/todos/{todo_id}")                   # 부분 수정 — 완료 체크·숨김처럼 일부 필드만 바꿀 때
@locked
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
    return replace_todo(todos, index, todo)


@app.delete("/todos/{todo_id}", status_code=204)  # 삭제 — 바로 지우지 않고 휴지통으로
@locked
def delete_todo(todo_id: int) -> None:
    todos = load_todos()
    index = find_index(todos, todo_id)
    move_to_trash([(index, todos.pop(index))])
    save_todos(todos)


# ---- 휴지통 ----

@app.get("/trash")                               # 휴지통 — 최근에 지운 것부터
def get_trash() -> list[TrashedTodo]:
    return sorted(load_trash(), key=lambda t: t.deleted_at, reverse=True)


@app.post("/trash/restore")                      # 되돌리기 — 원래 자리로 (화면의 '실행 취소'도 이걸 쓴다)
@locked
def restore_todos(payload: IdsIn) -> list[TodoItem]:
    trash = load_trash()
    picked = [trash[find_index(trash, i)] for i in dict.fromkeys(payload.ids)]
    category_ids = {c.id for c in load_categories()}
    todos = load_todos()
    restored = []
    for t in sorted(picked, key=lambda t: t.position):  # 앞자리부터 끼워 넣어야 원래 순서가 된다
        todo = TodoItem(**t.model_dump(exclude={"deleted_at", "position"}))
        if todo.category_id not in category_ids:
            todo.category_id = None              # 그사이 분류가 지워졌으면 미분류로
        todos.insert(min(t.position, len(todos)), todo)
        restored.append(todo)
    save_todos(todos)
    save_trash([t for t in trash if t.id not in payload.ids])
    return restored


@app.delete("/trash/{todo_id}", status_code=204)  # 영구 삭제
@locked
def purge_todo(todo_id: int) -> None:
    trash = load_trash()
    del trash[find_index(trash, todo_id)]
    save_trash(trash)


@app.delete("/trash")                            # 휴지통 비우기
@locked
def empty_trash() -> dict[str, int]:
    count = len(load_trash())
    save_trash([])
    return {"deleted": count}


# ---- 내보내기 · 가져오기 ----

CSV_COLUMNS = ["id", "title", "description", "completed", "due_date", "category", "priority",
               "tags", "repeat", "subtasks_done", "subtasks_total", "created_at"]


@app.get("/export")                              # 백업 — JSON(그대로 다시 가져올 수 있음) 또는 CSV(엑셀용)
@locked
def export_data(format: Literal["json", "csv"] = "json") -> Response:
    todos, categories = load_todos(), load_categories()
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    if format == "json":
        body = {"todos": [t.model_dump(mode="json") for t in todos],
                "categories": [c.model_dump(mode="json") for c in categories]}
        return JSONResponse(body, headers={"Content-Disposition": f'attachment; filename="todos-{stamp}.json"'})
    names = {c.id: c.name for c in categories}
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    for t in todos:
        writer.writerow([
            t.id, t.title, t.description, t.completed, t.due_date.isoformat() if t.due_date else "",
            names.get(t.category_id, ""), t.priority, ";".join(t.tags), t.repeat,
            sum(s.done for s in t.subtasks), len(t.subtasks), t.created_at.isoformat() if t.created_at else "",
        ])
    return Response("﻿" + out.getvalue(), media_type="text/csv; charset=utf-8",  # BOM: 엑셀에서 한글이 안 깨지게
                    headers={"Content-Disposition": f'attachment; filename="todos-{stamp}.csv"'})


@app.post("/import")                             # 가져오기 — 지금 목록 뒤에 덧붙인다 (분류는 이름으로 맞추고, 없으면 만든다)
@locked
def import_data(payload: ImportIn) -> dict[str, int]:
    categories = load_categories()
    by_name = {c.name: c.id for c in categories}
    created = 0
    for c in payload.categories:
        if c.name not in by_name:
            by_name[c.name] = max((x.id for x in categories), default=0) + 1
            categories.append(Category(id=by_name[c.name], name=c.name))
            created += 1
    source_names = {c.id: c.name for c in payload.categories}  # 내보낸 쪽의 분류 id → 이름
    todos = load_todos()
    new_id, now = next_id(todos), datetime.now()
    for i, t in enumerate(payload.todos):
        category_id = by_name.get(source_names.get(t.category_id))
        todos.append(TodoItem(**{**t.model_dump(), "category_id": category_id},
                              id=new_id + i, created_at=now, updated_at=now))
    save_categories(categories)
    save_todos(todos)
    return {"todos": len(payload.todos), "categories": created}


@app.get("/categories")                          # 분류 목록
def get_categories() -> list[Category]:
    return load_categories()


@app.post("/categories", status_code=201)        # 분류 추가
@locked
def create_category(payload: CategoryIn) -> Category:
    categories = load_categories()
    name = payload.name
    check_unique_name(categories, name)
    category = Category(id=max((c.id for c in categories), default=0) + 1, name=name)
    save_categories(categories + [category])
    return category


@app.put("/categories/{category_id}")            # 분류 이름 변경
@locked
def update_category(category_id: int, payload: CategoryIn) -> Category:
    categories = load_categories()
    index = find_index(categories, category_id, "Category")
    name = payload.name
    check_unique_name(categories, name, exclude_id=category_id)
    categories[index] = Category(id=category_id, name=name)
    save_categories(categories)
    return categories[index]


@app.delete("/categories/{category_id}", status_code=204)  # 분류 삭제 — 속한 할 일은(휴지통 것도) 미분류로 옮긴다
@locked
def delete_category(category_id: int) -> None:
    categories = load_categories()
    del categories[find_index(categories, category_id, "Category")]
    save_categories(categories)
    for load, save in ((load_todos, save_todos), (load_trash, save_trash)):
        items = load()
        for item in items:
            if item.category_id == category_id:
                item.category_id = None
        save(items)


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
        items.add_metric(["trashed"], len(load_trash()))
        yield items
        pending = GaugeMetricFamily("todo_pending_by_priority", "Pending to-do items by priority", labels=["priority"])
        for priority in PRIORITY_RANK:
            pending.add_metric([priority], sum(not t.completed and t.priority == priority for t in todos))
        yield pending


REGISTRY.register(TodoCollector())


@app.get("/", include_in_schema=False)           # 화면 서빙
def read_root() -> FileResponse:
    return FileResponse(INDEX_FILE, media_type="text/html")
