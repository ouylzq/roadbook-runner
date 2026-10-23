"""浏览器驱动：把 roadbook 能力词表绑到 Playwright 上。

roadbook 的资产层只说能力名、不说工具名，所以「加一个运行时」只需要写这一个文件。
本运行时无人值守跑，按 roadbook runtimes/playwright-mcp.md 的规矩：
优先按引用点，坐标是退守；截图只留档，永远不是判据。

驱动不知道看板的存在：实时帧通过 `on_frame` 回调交出去，谁接、怎么接是调用方的事。
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Browser as PwBrowser
from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from .observation import Observation

SNAPSHOT_JS = (Path(__file__).parent / "snapshot.js").read_text(encoding="utf-8")
INNER_TEXT_JS = "() => (document.body ? document.body.innerText : '').replace(/\\n{3,}/g, '\\n\\n')"

NO_BROWSER = "这一步没有浏览器可用（当前任务只写资产、不试跑）。别再调浏览器工具。"

FrameSink = Callable[[bytes], None]

FIND_MAX_HITS = 20
# JS 里满是花括号，别用 f-string/% 拼——用占位符替换更稳
FIND_JS = """(q) => { const out=[];
     document.querySelectorAll('[data-qa-ref]').forEach(el=>{
       const t=(el.innerText||el.value||el.placeholder||
                el.getAttribute('aria-label')||'').trim();
       if(t.toLowerCase().includes(q.toLowerCase()))
         out.push({ref:el.getAttribute('data-qa-ref'), text:t.slice(0,80)});
     }); return out.slice(0, __MAX__); }""".replace("__MAX__", str(FIND_MAX_HITS))

DEFAULT_VIEWPORT = (1280, 800)
ACTION_SETTLE_MS = 400
NAVIGATE_SETTLE_MS = 500
SCROLL_SETTLE_MS = 1200          # 平滑滚动有动画，中间帧不可信
CLICK_TIMEOUT_MS = 5000
MAX_WAIT_SECONDS = 15
EVAL_RESULT_MAX_CHARS = 2000
CONSOLE_LINE_MAX_CHARS = 400
LIVE_JPEG_QUALITY = 55
SCREENCAST_JPEG_QUALITY = 60


class Browser:
    """一个浏览器会话。用 with 管生命周期，退出时录屏才落盘。

    storage_state 让「独立裁判」成立：裁判拿到全新的会话 + 全新的对话，
    但继承被测应用已经落盘的状态（localStorage/cookie）——否则验持久化的场景
    会因为换了个空 context 而必然误判（2026-09-16 实测踩过）。
    """

    def __init__(
        self,
        run_dir: Path,
        *,
        headless: bool = True,
        viewport: tuple[int, int] = DEFAULT_VIEWPORT,
        storage_state: Path | None = None,
        vercel_bypass_secret: str = "",
        vercel_bypass_hosts: tuple[str, ...] = (),
        on_frame: FrameSink | None = None,
        dedupe_observations: bool = True,
    ):
        self.run_dir = run_dir
        self.headless = headless
        self.viewport = viewport
        self.storage_state = storage_state if storage_state and storage_state.exists() else None
        self.vercel_bypass_secret = vercel_bypass_secret
        self.vercel_bypass_hosts = frozenset(host.lower() for host in vercel_bypass_hosts)
        self.on_frame = on_frame
        self.dedupe_observations = dedupe_observations

        self._last_observed: str | None = None   # 上次 observe 渲染出来的正文，用来去重
        self._console_errors: list[str] = []
        self._shot_seq = 0
        self._pw: Playwright | None = None
        self._browser: PwBrowser | None = None
        self._ctx: BrowserContext | None = None
        self._cdp: Any = None
        self._page: Page | None = None

    # ---- 生命周期 ---------------------------------------------------------
    def __enter__(self) -> Browser:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "shots").mkdir(exist_ok=True)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        width, height = self.viewport
        self._ctx = self._browser.new_context(
            viewport={"width": width, "height": height},
            locale="zh-CN",
            storage_state=str(self.storage_state) if self.storage_state else None,
            record_video_dir=str(self.run_dir / "video"),
            record_video_size={"width": width, "height": height},
        )
        if self.vercel_bypass_secret and self.vercel_bypass_hosts:
            self._ctx.route("**/*", self._route_with_vercel_bypass)
        self._page = self._ctx.new_page()
        self._page.on("console", self._on_console)
        self._page.on("pageerror", lambda e: self._console_errors.append(f"[pageerror] {e}"))
        self._start_screencast()
        return self

    def _route_with_vercel_bypass(self, route: Any) -> None:
        """只给白名单内的 Vercel 靶子加 bypass；第三方资源绝不能收到密钥。"""
        host = (urlsplit(route.request.url).hostname or "").lower()
        if host not in self.vercel_bypass_hosts:
            route.continue_()
            return
        headers = dict(route.request.headers)
        headers["x-vercel-protection-bypass"] = self.vercel_bypass_secret
        route.continue_(headers=headers)

    def __exit__(self, *exc: object) -> None:
        try:
            if self._cdp:
                # 收尾期的推流错误不该盖住真正的异常
                with contextlib.suppress(Exception):
                    self._cdp.send("Page.stopScreencast")
            if self._ctx:
                self._ctx.close()   # close 之后录屏才落盘
        finally:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser 还没进入 with 块")
        return self._page

    def _start_screencast(self) -> None:
        """CDP 实时推流。录屏要等 context 关闭才落盘，看板等不了，所以另开一路。

        帧只在 Playwright 处理事件时被泵出来——浏览器真在动的时候。
        模型思考那几秒画面会停住，靠 observe() 里补推的一帧兜底。
        """
        if not self.on_frame or not self._ctx:
            return
        try:
            self._cdp = self._ctx.new_cdp_session(self.page)

            def deliver(params: dict[str, Any]) -> None:
                try:
                    self.on_frame(base64.b64decode(params["data"]))  # type: ignore[misc]
                    self._cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
                except Exception:
                    pass

            self._cdp.on("Page.screencastFrame", deliver)
            width, height = self.viewport
            self._cdp.send("Page.startScreencast", {
                "format": "jpeg", "quality": SCREENCAST_JPEG_QUALITY,
                "maxWidth": width, "maxHeight": height, "everyNthFrame": 1,
            })
        except Exception:
            self._cdp = None

    def _on_console(self, msg: Any) -> None:
        if msg.type in ("error", "warning"):
            self._console_errors.append(f"[{msg.type}] {msg.text}"[:CONSOLE_LINE_MAX_CHARS])

    # ---- 产物 --------------------------------------------------------------
    def save_storage_state(self, path: Path) -> Path | None:
        """把被测应用的落盘状态导出，交给裁判继承。"""
        if not self._ctx:
            return None
        try:
            self._ctx.storage_state(path=str(path))
            return path
        except Exception:
            return None

    def video_path(self) -> Path | None:
        videos = sorted((self.run_dir / "video").glob("*.webm"))
        return videos[0] if videos else None

    def screenshot(self) -> str:
        self._shot_seq += 1
        path = self.run_dir / "shots" / f"{self._shot_seq:03d}.png"
        self.page.screenshot(path=str(path))
        return str(path)

    # ---- 能力词表 -----------------------------------------------------------
    def observe(self, *, shoot: bool = True) -> Observation:
        self._push_live_frame()
        snap = self.page.evaluate(SNAPSHOT_JS)
        text = self.page.evaluate(INNER_TEXT_JS) or ""
        return Observation(
            url=snap["url"], title=snap["title"], tree=snap["tree"], text=text,
            ref_count=snap["ref_count"], console_errors=list(self._console_errors),
            screenshot_path=self.screenshot() if shoot else None,
        )

    def observe_text(self) -> str:
        """observe 工具要喂给模型的那段文本。页面跟上次一模一样时只回一句话。

        2026-09-21 实测：页面定格之后 agent 又 observe 了十几次，每次都把同样的
        一大坨结构重新塞进上下文——那趟跑批的上下文从 13k 涨到 123k，80% 的 token
        烧在这上面。回一句「跟上次一样」既省 token，也让「没进展」这件事直接说给模型听
        （引用编号是按遍历顺序生成的，页面没变则编号不变，所以旧的 ref 仍然有效）。
        """
        rendered = self.observe().render()
        if self.dedupe_observations and rendered == self._last_observed:
            return ("页面跟你上次 observe 时**一模一样**，没有任何变化——"
                    "结构、文本、URL 全都没动，之前拿到的 {ref_N} 编号继续有效。\n"
                    "别再 observe 同一个页面等它自己变了：要么去做点会改变页面的操作，"
                    "要么按你已有的信息收口。")
        self._last_observed = rendered
        return rendered

    def fingerprint(self) -> str:
        """当前页面的指纹，给「原地踏步」检测用。不截图、不落盘，比一次 observe 便宜。

        取 url + 标题 + 结构树 + 可见文本，**不取控制台错误**——那是累加的，
        一旦出现一条就永远在，反而会让指纹在页面没变时也抖一下。
        """
        snap = self.page.evaluate(SNAPSHOT_JS)
        text = self.page.evaluate(INNER_TEXT_JS) or ""
        raw = f"{snap['url']}\n{snap['title']}\n{snap['tree']}\n{text}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _push_live_frame(self) -> None:
        """每次观察补推一帧：CDP screencast 在模型思考期间没有事件可泵。"""
        if not self.on_frame:
            return
        with contextlib.suppress(Exception):   # 推流是锦上添花，失败不影响观察本身
            self.on_frame(self.page.screenshot(type="jpeg", quality=LIVE_JPEG_QUALITY))

    def current_url(self) -> str:
        return self.page.url

    def navigate(self, url: str) -> str:
        self.page.goto(url, wait_until="domcontentloaded")
        self.page.wait_for_timeout(NAVIGATE_SETTLE_MS)
        return f"已打开 {self.page.url}"

    def click_ref(self, ref: str) -> str:
        self.page.click(_ref_selector(ref), timeout=CLICK_TIMEOUT_MS)
        self.page.wait_for_timeout(ACTION_SETTLE_MS)
        return f"已点击 {ref}"

    def click_xy(self, x: int, y: int) -> str:
        self.page.mouse.click(x, y)
        self.page.wait_for_timeout(ACTION_SETTLE_MS)
        return f"已点击坐标 ({x}, {y})"

    def type_text(self, ref: str, text: str, *, submit: bool = False) -> str:
        locator = self.page.locator(_ref_selector(ref))
        locator.click(timeout=CLICK_TIMEOUT_MS)
        locator.fill("")
        locator.type(text, delay=20)
        if submit:
            locator.press("Enter")
        self.page.wait_for_timeout(ACTION_SETTLE_MS)
        return f"已在 {ref} 输入 {text!r}" + ("（并回车提交）" if submit else "")

    def set_value(self, ref: str, value: str) -> str:
        self.page.locator(_ref_selector(ref)).fill(value, timeout=CLICK_TIMEOUT_MS)
        self.page.wait_for_timeout(ACTION_SETTLE_MS)
        return f"已给 {ref} 赋值 {value!r}"

    def set_files(self, ref: str | None, selector: str | None, paths: list[str]) -> str:
        """把本地文件直接装进文件输入框（Playwright 原生 setInputFiles）。

        字节走 Playwright 不走模型输出——LLM 手抄 base64 会损坏（2026-10-22 实测两次转抄
        全坏），长文件还会撞单次输出上限。这是文件注入的唯一正规路径，base64 手抄降级为退守。
        隐藏的 input[type=file] 不进观察、拿不到 ref，所以也接受 CSS 选择器（取第一个匹配——
        onboarding 页有 3 个隐藏文件输入框，第一个即正主）。
        """
        if ref:
            locator = self.page.locator(_ref_selector(ref))
        elif selector:
            locator = self.page.locator(selector).first
        else:
            raise ValueError("set_files 需要 ref 或 selector 其一")
        locator.set_input_files(paths, timeout=CLICK_TIMEOUT_MS)
        self.page.wait_for_timeout(ACTION_SETTLE_MS)
        names = "、".join(p.rsplit("/", 1)[-1] for p in paths)
        return f"已把 {len(paths)} 个文件装进 {'ref ' + ref if ref else selector}：{names}"

    def find(self, query: str) -> str:
        hits = self.page.evaluate(FIND_JS, query)
        if not hits:
            return f"没找到含「{query}」的可交互元素（先 observe 刷新引用）"
        return "\n".join(f"{h['ref']}: {h['text']}" for h in hits)

    def eval_js(self, expr: str) -> str:
        try:
            value = self.page.evaluate(f"() => ({expr})")
        except Exception as e:
            return f"脚本报错: {e}"
        return json.dumps(value, ensure_ascii=False)[:EVAL_RESULT_MAX_CHARS]

    def call_js(self, fn: str, arg: Any) -> Any:
        """调一个 JS 函数并把参数传进去，返回原始值（不 json 化、不截断）。

        跟 `eval_js` 的区别：那个是把表达式拼进字符串里求值，参数只能自己拼进去——
        搜索词里有引号或反斜杠就会把脚本拼坏。这里走 Playwright 的参数通道，天然安全。
        """
        try:
            return self.page.evaluate(fn, arg)
        except Exception as e:
            return f"脚本报错: {e}"

    def wait(self, seconds: float) -> str:
        self.page.wait_for_timeout(int(min(seconds, MAX_WAIT_SECONDS) * 1000))
        return f"已等待 {seconds} 秒"

    def scroll(self, direction: str = "down", amount: int = 3) -> str:
        step = {"down": 400, "up": -400}.get(direction, 400)
        self.page.mouse.wheel(0, step * amount)
        self.page.wait_for_timeout(SCROLL_SETTLE_MS)
        return f"已向{direction}滚动 {amount} 格"


def _ref_selector(ref: str) -> str:
    return f'[data-qa-ref="{ref}"]'
