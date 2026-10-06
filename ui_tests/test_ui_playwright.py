# Playwright 로 실제 브라우저(Chrome)를 띄워 화면을 조작하는 UI 테스트
from datetime import datetime, timedelta

import pytest
from playwright.sync_api import Page, expect, sync_playwright


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")   # 설치된 Chrome 사용 (별도 브라우저 다운로드 없음)
        yield browser
        browser.close()


@pytest.fixture
def page(browser, base_url) -> Page:
    context = browser.new_context(viewport={"width": 520, "height": 900}, locale="ko-KR")
    page = context.new_page()
    page.goto(base_url)
    yield page
    context.close()


def todo_row(page, title):
    return page.locator("#todo-list li:not(.group)", has_text=title)


def tab(page, label):
    return page.locator("#tabs button", has_text=label)


def add_todo(page, title, description=""):
    page.fill("#title", title)
    page.fill("#description", description)
    page.click("#todo-form button[type=submit]")
    expect(todo_row(page, title)).to_be_visible()


def test_add_todo(page, screenshot_path):
    expect(page.locator("#empty")).to_be_visible()                       # 처음엔 빈 목록 안내
    add_todo(page, "과제 제출", "DevOps 보고서")
    expect(todo_row(page, "과제 제출")).to_contain_text("DevOps 보고서")
    expect(page.locator("#subtitle")).to_have_text("0 / 1개 완료")
    expect(page.locator("#title")).to_have_value("")                     # 추가 후 입력칸 초기화
    page.screenshot(animations="disabled", path=screenshot_path("playwright_01_add"))


def test_add_todo_requires_title(page):
    page.click("#todo-form button[type=submit]")                        # 제목 없이 제출
    expect(page.locator("#todo-list li")).to_have_count(0)               # 브라우저 required 검사로 막힘


def test_complete_todo(page, screenshot_path):
    add_todo(page, "장보기")
    todo_row(page, "장보기").locator("input[type=checkbox]").check()
    expect(todo_row(page, "장보기")).to_have_class("done")
    expect(page.locator("#subtitle")).to_have_text("1 / 1개 완료")
    expect(page.locator("#clear-completed")).to_have_text("🧹 완료된 항목 1개 모두 지우기")
    page.screenshot(animations="disabled", path=screenshot_path("playwright_02_complete"))


def test_overdue_and_soon_badges(page, api):
    now = datetime.now()
    api("POST", "/todos", {"title": "지난 일", "due_date": (now - timedelta(hours=1)).isoformat(timespec="minutes")})
    api("POST", "/todos", {"title": "임박한 일", "due_date": (now + timedelta(days=1)).isoformat(timespec="minutes")})
    page.reload()
    expect(todo_row(page, "지난 일").locator(".due")).to_have_class("due overdue")
    expect(todo_row(page, "임박한 일").locator(".due")).to_have_class("due soon")


def test_search(page, api, screenshot_path):
    api("POST", "/todos", {"title": "장보기", "description": "Milk"})
    api("POST", "/todos", {"title": "과제", "description": "DevOps 보고서"})
    page.reload()
    page.fill("#search", "milk")                                         # 대소문자 무시, 설명에서도 검색
    expect(page.locator("#todo-list li")).to_have_count(1)
    expect(todo_row(page, "장보기")).to_be_visible()
    page.screenshot(animations="disabled", path=screenshot_path("playwright_03_search"))
    page.fill("#search", "없는검색어")
    expect(page.locator("#empty")).to_have_text("검색 결과가 없어요 🔍")
    page.fill("#search", "")
    expect(page.locator("#todo-list li")).to_have_count(2)              # 검색어를 지우면 전체 복구


def test_hide_groups_by_category(page, api, screenshot_path):
    school = api("POST", "/categories", {"name": "학교"})
    home = api("POST", "/categories", {"name": "집"})
    api("POST", "/todos", {"title": "과제", "category_id": school["id"]})
    api("POST", "/todos", {"title": "청소", "category_id": home["id"]})
    api("POST", "/todos", {"title": "독서"})
    api("POST", "/todos", {"title": "운동"})
    page.reload()
    expect(tab(page, "숨김")).to_have_count(0)                           # 숨긴 항목이 없으면 탭도 없음

    for title in ("청소", "독서", "과제"):
        todo_row(page, title).get_by_title("숨기기").click()
        expect(todo_row(page, title)).to_have_count(0)                  # 일반 목록에서 사라짐
    expect(page.locator("#todo-list li")).to_have_count(1)              # 운동만 남음

    tab(page, "숨김").click()
    groups = page.locator("#todo-list li.group")                         # 분류 순서대로, 미분류는 맨 뒤
    expect(groups).to_have_text(["📁 학교 · 1", "📁 집 · 1", "📁 미분류 · 1"])
    page.screenshot(animations="disabled", path=screenshot_path("playwright_04_hidden_tab"))


def test_unhide(page, api):
    api("POST", "/todos", {"title": "숨긴 일", "hidden": True})
    page.reload()
    tab(page, "숨김").click()
    todo_row(page, "숨긴 일").get_by_title("다시 보이기").click()
    expect(tab(page, "숨김")).to_have_count(0)                           # 다 꺼내면 탭이 사라지고
    expect(tab(page, "전체")).to_have_class("tab active")                # 전체 탭으로 돌아옴
    expect(todo_row(page, "숨긴 일")).to_be_visible()


def test_clear_completed(page, api, screenshot_path):
    api("POST", "/todos", {"title": "끝낸 일", "completed": True})
    api("POST", "/todos", {"title": "남은 일"})
    api("POST", "/todos", {"title": "숨긴 끝낸 일", "completed": True, "hidden": True})
    page.reload()

    page.once("dialog", lambda dialog: dialog.dismiss())                 # 확인 창에서 취소 → 그대로
    page.click("#clear-completed")
    expect(todo_row(page, "끝낸 일")).to_be_visible()

    page.once("dialog", lambda dialog: dialog.accept())                  # 확인 → 삭제
    page.click("#clear-completed")
    expect(todo_row(page, "끝낸 일")).to_have_count(0)
    expect(todo_row(page, "남은 일")).to_be_visible()
    expect(page.locator("#list-footer")).to_be_hidden()
    tab(page, "숨김").click()
    expect(todo_row(page, "숨긴 끝낸 일")).to_be_visible()              # 숨긴 완료 항목은 남아 있음
    page.screenshot(animations="disabled", path=screenshot_path("playwright_05_clear_completed"))


def test_add_category_and_filter(page, api, screenshot_path):
    api("POST", "/todos", {"title": "미분류 할 일"})
    page.reload()
    page.once("dialog", lambda dialog: dialog.accept("학교"))            # "+ 분류" 의 prompt 에 이름 입력
    tab(page, "+ 분류").click()
    expect(tab(page, "학교")).to_have_class("tab active")                # 새 분류 탭이 선택됨
    expect(page.locator("#category-name")).to_have_text("📁 학교")
    expect(page.locator("#category")).to_have_value("1")                 # 추가 폼 기본 분류 = 보고 있는 분류

    add_todo(page, "과제")
    expect(todo_row(page, "미분류 할 일")).to_have_count(0)             # 학교 탭에는 학교 할 일만
    tab(page, "미분류").click()
    expect(todo_row(page, "미분류 할 일")).to_be_visible()
    page.screenshot(animations="disabled", path=screenshot_path("playwright_06_category"))


def test_delete_todo(page, api):
    api("POST", "/todos", {"title": "지울 일"})
    page.reload()
    page.once("dialog", lambda dialog: dialog.accept())
    todo_row(page, "지울 일").get_by_title("삭제").click()
    expect(page.locator("#empty")).to_be_visible()
