"""Start the bundled Roadbook tutorial or run its scenario with the real Runner."""

import argparse
import json
import os
import subprocess
import sys
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock, Thread
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

DEMO = Path(__file__).resolve().parent
ROOT = DEMO.parents[1]
SKILLS = DEMO / "skills"
SCENARIO = SKILLS / "roadbook-demo" / "scenarios" / "save-task"
HOST, PORT = "127.0.0.1", 4173
URL = f"http://{HOST}:{PORT}"
VARIANTS = ("normal", "renamed", "lost-write")
FILES = {
    "/": (DEMO / "tour.html", "text/html"),
    "/tasks": (DEMO / "tasks.html", "text/html"),
    "/demo.css": (DEMO / "demo.css", "text/css"),
    "/tasks.js": (DEMO / "tasks.js", "text/javascript"),
    **{f"/files/{name}": (SCENARIO / name, "text/plain")
       for name in ("intent.md", "playbook.md", "expect.yaml")},
}


class DemoServer(ThreadingHTTPServer):
    def __init__(self, variant):
        self.variant = variant
        self.tasks = {}
        self.lock = Lock()
        super().__init__((HOST, PORT), Handler)


class Handler(BaseHTTPRequestHandler):
    def send_body(self, body, content_type, cookie=None):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", f"roadbook_demo={cookie}; Path=/; HttpOnly; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload):
        self.send_body(json.dumps(payload).encode("utf-8"), "application/json")

    def route(self):
        url = urlsplit(self.path)
        variant = parse_qs(url.query).get("variant", [self.server.variant])[0]
        return url.path, variant

    def session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        return cookie["roadbook_demo"].value if "roadbook_demo" in cookie else None

    def do_GET(self):
        path, variant = self.route()
        if variant not in VARIANTS:
            self.send_error(HTTPStatus.NOT_FOUND)
        elif path == "/api/task":
            with self.server.lock:
                task = self.server.tasks.get((self.session(), variant))
            self.send_json({"title": task, "variant": variant})
        elif path in FILES:
            file, content_type = FILES[path]
            cookie = uuid4().hex if path == "/tasks" and not self.session() else None
            self.send_body(file.read_bytes(), content_type, cookie)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path, variant = self.route()
        if path != "/api/task" or variant not in VARIANTS:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        session = self.session()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not session or not 1 <= length <= 1024:
                raise ValueError
            payload = json.loads(self.rfile.read(length))
            title = payload.get("title") if isinstance(payload, dict) else None
            if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
                raise ValueError
        except (ValueError, UnicodeError):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        title = title.strip()
        if variant != "lost-write":
            with self.server.lock:
                self.server.tasks[(session, variant)] = title
        self.send_json({"title": title})

    def log_message(self, format, *args):
        pass


def run_runner(mode):
    from dotenv import dotenv_values

    settings = {**dotenv_values(ROOT / ".env"), **os.environ}
    required = ("QA_AGENT_BASE_URL", "QA_AGENT_API_KEY", "QA_AGENT_MODEL")
    missing = [name for name in required if not settings.get(name)]
    if missing:
        print("请在项目根目录的 .env 配置：" + "、".join(missing), file=sys.stderr)
        return 2

    # Use the real roles and the user's selected model for both phases.
    with TemporaryDirectory(prefix="roadbook-demo-agents-") as directory:
        agents = Path(directory)
        for name in ("agent.md", "judge.md"):
            text = (ROOT / "agents" / name).read_text(encoding="utf-8")
            opening, frontmatter, body = text.split("---", 2)
            frontmatter = "\n".join(line for line in frontmatter.split("\n")
                                    if not line.startswith("model:"))
            (agents / name).write_text("---".join((opening, frontmatter, body)), encoding="utf-8")
        env = {
            **os.environ,
            "QA_AGENT_SKILLS": str(SKILLS),
            "QA_AGENT_DEFAULT_SKILL": "roadbook-demo",
            "QA_AGENT_AGENTS": str(agents),
            "QA_AGENT_PROMPTS": str(ROOT / "prompts"),
            "QA_AGENT_TARGET_URL": URL + "/tasks",
            "QA_AGENT_TEST_ENV_URL": URL + "/tasks",
            "QA_AGENT_ENV_PROFILE": "test",
            "QA_AGENT_WEBHOOK_URL": "",
            "QA_AGENT_PROMPT_CACHE": "off",
            "QA_AGENT_ATOM_MEMORY": "off",
            "PYTHONUTF8": "1",
        }
        args = ["run", "save-task"] if mode == "run" else ["serve"]
        if mode == "dashboard":
            print("Runner 看板：http://127.0.0.1:8900", flush=True)
        return subprocess.call([sys.executable, str(ROOT / "run.py"), *args], cwd=ROOT, env=env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preview", "run", "dashboard"), nargs="?", default="preview")
    parser.add_argument("--variant", choices=VARIANTS, default="normal")
    args = parser.parse_args()
    try:
        server = DemoServer(args.variant)
    except OSError as exc:
        parser.exit(2, f"无法在 {URL} 启动演示。请关闭占用 {PORT} 端口的程序。\n{exc}\n")
    try:
        with server:
            print(f"Roadbook Demo：{URL}/", flush=True)
            if args.mode == "preview":
                server.serve_forever()
                return 0
            worker = Thread(target=server.serve_forever)
            worker.start()
            try:
                return run_runner(args.mode)
            except ModuleNotFoundError as exc:
                if exc.name != "dotenv":
                    raise
                print("请先执行 python -m pip install -r requirements.txt", file=sys.stderr)
                return 2
            finally:
                server.shutdown()
                worker.join()
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
