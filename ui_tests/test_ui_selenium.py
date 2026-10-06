# Selenium(WebDriver)으로 같은 화면을 조작하는 UI 테스트
# Playwright 와 달리 자동 대기가 없으므로 WebDriverWait 로 화면이 바뀔 때까지 직접 기다린다
import pytest
from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

TIMEOUT = 5


@pytest.fixture(scope="module")
def driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=520,900")
    options.add_argument("--lang=ko-KR")
    driver = webdriver.Chrome(options=options)   # Selenium Manager 가 Chrome 버전에 맞는 드라이버를 준비
    yield driver
    driver.quit()


@pytest.fixture
def page(driver, base_url):
    driver.get(base_url)
    wait_for(driver, lambda d: d.find_element(By.ID, "tabs").text)       # 첫 렌더링(탭 생성)까지 대기
    return driver


def wait_for(driver, condition):
    # 화면은 데이터를 다시 불러올 때마다 목록을 새로 그린다 → 그 사이 사라진 요소(stale)는 무시하고 재시도
    wait = WebDriverWait(driver, TIMEOUT, ignored_exceptions=[StaleElementReferenceException])
    return wait.until(condition)


def rows(driver):
    return driver.find_elements(By.CSS_SELECTOR, "#todo-list li:not(.group)")


def row_titles(driver):
    return [li.find_element(By.CLASS_NAME, "title").text for li in rows(driver)]


def todo_row(driver, title):
    return wait_for(driver, lambda d: next((li for li in rows(d) if li.find_element(By.CLASS_NAME, "title").text == title), None))


def wait_titles(driver, expected):
    # 목록이 기대한 제목들로 바뀔 때까지 대기
    wait_for(driver, lambda d: row_titles(d) == expected)


def click_tab(driver, label):
    wait_for(driver, lambda d: next((b for b in d.find_elements(By.CSS_SELECTOR, "#tabs button") if label in b.text), None)).click()


def tab_labels(driver):
    return [b.text for b in driver.find_elements(By.CSS_SELECTOR, "#tabs button")]


def add_todo(driver, title, description=""):
    driver.find_element(By.ID, "title").send_keys(title)
    driver.find_element(By.ID, "description").send_keys(description)
    driver.find_element(By.CSS_SELECTOR, "#todo-form button[type=submit]").click()
    todo_row(driver, title)


def answer_dialog(driver, accept=True, text=None):
    alert = wait_for(driver, EC.alert_is_present())
    if text is not None:
        alert.send_keys(text)
    alert.accept() if accept else alert.dismiss()


def test_add_todo(page, screenshot_path):
    assert page.find_element(By.ID, "empty").is_displayed()
    add_todo(page, "과제 제출", "DevOps 보고서")
    assert "DevOps 보고서" in todo_row(page, "과제 제출").text
    wait_for(page, EC.text_to_be_present_in_element((By.ID, "subtitle"), "0 / 1개 완료"))
    assert page.find_element(By.ID, "title").get_attribute("value") == ""
    page.save_screenshot(screenshot_path("selenium_01_add"))


def test_complete_todo(page):
    add_todo(page, "장보기")
    todo_row(page, "장보기").find_element(By.CSS_SELECTOR, "input[type=checkbox]").click()
    wait_for(page, lambda d: todo_row(d, "장보기").get_attribute("class") == "done")
    wait_for(page, EC.text_to_be_present_in_element((By.ID, "clear-completed"), "완료된 항목 1개"))


def test_search(page, api, screenshot_path):
    api("POST", "/todos", {"title": "장보기", "description": "Milk"})
    api("POST", "/todos", {"title": "과제", "description": "DevOps 보고서"})
    page.refresh()
    wait_titles(page, ["장보기", "과제"])
    search = page.find_element(By.ID, "search")
    search.send_keys("보고서")
    wait_titles(page, ["과제"])
    page.save_screenshot(screenshot_path("selenium_02_search"))
    search.send_keys("없음")
    wait_for(page, EC.text_to_be_present_in_element((By.ID, "empty"), "검색 결과가 없어요"))


def test_hide_and_unhide(page, api, screenshot_path):
    school = api("POST", "/categories", {"name": "학교"})
    api("POST", "/todos", {"title": "과제", "category_id": school["id"]})
    api("POST", "/todos", {"title": "독서"})
    page.refresh()
    wait_titles(page, ["과제", "독서"])
    assert not any("숨김" in label for label in tab_labels(page))

    todo_row(page, "과제").find_element(By.CSS_SELECTOR, "[title='숨기기']").click()
    wait_titles(page, ["독서"])
    click_tab(page, "숨김")
    wait_titles(page, ["과제"])
    group = page.find_element(By.CSS_SELECTOR, "#todo-list li.group")
    assert group.text == "📁 학교 · 1"
    page.save_screenshot(screenshot_path("selenium_03_hidden_tab"))

    todo_row(page, "과제").find_element(By.CSS_SELECTOR, "[title='다시 보이기']").click()
    wait_titles(page, ["과제", "독서"])                                  # 전체 탭으로 돌아옴
    assert not any("숨김" in label for label in tab_labels(page))


def test_clear_completed(page, api):
    api("POST", "/todos", {"title": "끝낸 일", "completed": True})
    api("POST", "/todos", {"title": "남은 일"})
    page.refresh()
    wait_titles(page, ["끝낸 일", "남은 일"])

    page.find_element(By.ID, "clear-completed").click()
    answer_dialog(page, accept=False)                                    # 취소 → 그대로
    wait_titles(page, ["끝낸 일", "남은 일"])

    page.find_element(By.ID, "clear-completed").click()
    answer_dialog(page, accept=True)                                     # 확인 → 삭제
    wait_titles(page, ["남은 일"])
    wait_for(page, EC.invisibility_of_element_located((By.ID, "list-footer")))


def test_add_category(page, screenshot_path):
    click_tab(page, "+ 분류")
    answer_dialog(page, text="학교")                                     # prompt 에 이름 입력
    wait_for(page, EC.text_to_be_present_in_element((By.ID, "category-name"), "📁 학교"))
    add_todo(page, "과제")
    click_tab(page, "전체")
    assert "📁 학교" in todo_row(page, "과제").text                     # 전체 탭에서는 분류 표시
    page.save_screenshot(screenshot_path("selenium_04_category"))
