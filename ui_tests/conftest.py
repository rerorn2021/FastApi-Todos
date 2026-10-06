# UI 테스트 공통 설정 — 임시 데이터 폴더로 앱 서버를 띄우고, 테스트마다 데이터를 비운다
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT.parent / "fastapi-app"
SCREENSHOT_DIR = ROOT / "report" / "screenshots"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("data")      # 실제 todo.json 은 건드리지 않는다


@pytest.fixture(scope="session")
def base_url(data_dir):
    # DATA_DIR 로 데이터 위치를 임시 폴더로 돌려서 서버 실행
    port = free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=APP_DIR, env={**os.environ, "DATA_DIR": str(data_dir)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):                          # 최대 10초 기다림
        try:
            urllib.request.urlopen(url + "/todos", timeout=1)
            break
        except OSError:
            time.sleep(0.2)
    else:
        server.kill()
        pytest.fail("앱 서버가 뜨지 않음")
    yield url
    server.terminate()
    server.wait()


@pytest.fixture(autouse=True)
def clean_data(data_dir):
    # 서버는 요청마다 파일을 읽으므로, 파일만 비우면 테스트 간 데이터가 격리된다
    for name in ("todo.json", "categories.json"):
        (data_dir / name).write_text("[]", encoding="utf-8")


@pytest.fixture
def api(base_url):
    # 화면 조작 전에 필요한 데이터를 API로 바로 만들어 두는 도우미
    def call(method, path, body=None):
        request = urllib.request.Request(
            base_url + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read() or "null")
    return call


@pytest.fixture
def screenshot_path():
    # 보고서에 넣을 스크린샷 경로 (report/screenshots/<이름>.png)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    return lambda name: str(SCREENSHOT_DIR / f"{name}.png")
