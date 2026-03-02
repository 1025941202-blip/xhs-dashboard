"""
小红书养生赛道爬虫
─────────────────────────────────────────
依赖安装：
  pip install playwright
  playwright install chromium

运行方式：
  python scraper.py

首次运行会弹出浏览器，手动扫码登录小红书。
登录成功后 cookies 会保存到 cookies.json，后续自动登录。

数据保存到 data.json，看板自动读取最新数据。
─────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page, BrowserContext

# ────────── 配置 ──────────
KEYWORD = "养生"          # 搜索关键词（可改为：中医养生 / 睡眠 / 减脂 等）
MAX_NOTES = 100           # 最多抓取笔记数量
SCROLL_TIMES = 25         # 最多滚动次数
SCROLL_PAUSE = 2.0        # 每次滚动等待秒数（太快易被限流）
OUTPUT_FILE = "data.json"
COOKIES_FILE = "cookies.json"
# ─────────────────────────


def parse_num(text: str) -> int:
    """把 '1.2万' / '3.5k' / '1234' 解析为整数"""
    if not text:
        return 0
    text = text.strip().replace(",", "")
    try:
        if "亿" in text:
            return int(float(text.replace("亿", "")) * 100_000_000)
        if "万" in text:
            return int(float(text.replace("万", "")) * 10_000)
        if "k" in text.lower():
            return int(float(text.lower().replace("k", "")) * 1_000)
        return int(float(re.sub(r"[^\d.]", "", text)))
    except Exception:
        return 0


async def save_cookies(context: BrowserContext):
    cookies = await context.cookies()
    Path(COOKIES_FILE).write_text(json.dumps(cookies, ensure_ascii=False, indent=2))
    print(f"  ✅ Cookies 已保存到 {COOKIES_FILE}")


async def load_cookies(context: BrowserContext) -> bool:
    p = Path(COOKIES_FILE)
    if not p.exists():
        return False
    try:
        cookies = json.loads(p.read_text())
        await context.add_cookies(cookies)
        print(f"  ✅ 已加载本地 Cookies")
        return True
    except Exception as e:
        print(f"  ⚠️  Cookies 加载失败: {e}")
        return False


async def is_login_modal_visible(page: Page) -> bool:
    """检测登录弹窗是否可见"""
    indicators = [
        "text=登录后查看搜索结果",
        "text=手机号登录",
        "text=扫码登录",
        "text=可用 小红书 或",
    ]
    for sel in indicators:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                return True
        except Exception:
            pass
    return False


async def wait_for_login(page: Page, context: BrowserContext):
    """等待用户手动扫码登录"""
    print("\n🔐 请在弹出的浏览器窗口中扫码登录小红书...")
    print("   登录完成后脚本会自动继续（最多等待 3 分钟）\n")
    # 等待登录弹窗消失
    for _ in range(180):
        await asyncio.sleep(1)
        if not await is_login_modal_visible(page):
            break
    await asyncio.sleep(3)
    await save_cookies(context)
    print("  ✅ 登录成功，Cookies 已保存！")


async def check_login(page: Page) -> bool:
    """检查当前是否已登录（无登录弹窗即视为已登录）"""
    await asyncio.sleep(3)
    if await is_login_modal_visible(page):
        return False
    if "login" in page.url or "signin" in page.url:
        return False
    return True


# 养生相关关键词，用于过滤无关内容
HEALTH_KEYWORDS = [
    "养生", "健康", "中医", "食疗", "补肾", "脾胃", "排毒", "睡眠",
    "减脂", "减肥", "护肝", "艾灸", "泡脚", "祛湿", "气血", "补血",
    "体质", "穴位", "经络", "调理", "保健", "营养", "饮食", "瘦身",
    "失眠", "疲劳", "亚健康", "补气", "暖身", "抗老", "护肤", "排寒",
]

def is_health_related(title: str) -> bool:
    return any(kw in title for kw in HEALTH_KEYWORDS)


async def scroll_and_collect(page: Page) -> list[dict]:
    """滚动页面，收集所有笔记卡片的基础信息"""
    notes_map: dict[str, dict] = {}
    prev_count = 0
    no_new_rounds = 0

    for i in range(SCROLL_TIMES):
        # ── 提取当前可见卡片
        # 先找所有笔记链接，再取其父容器作为卡片
        link_els = await page.query_selector_all("a[href*='/explore/']")
        cards = []
        seen_hrefs = set()
        for link in link_els:
            href = await link.get_attribute("href") or ""
            if href in seen_hrefs:
                continue
            seen_hrefs.add(href)
            # 取最近的有意义父容器
            parent = await page.evaluate("""el => {
                let cur = el.parentElement;
                for (let i = 0; i < 6 && cur; i++) {
                    if (cur.querySelectorAll('img').length > 0 &&
                        cur.querySelectorAll('span, p').length > 0) {
                        return cur;
                    }
                    cur = cur.parentElement;
                }
                return el;
            }""", link)
            if parent:
                cards.append(link)  # 用 link 自身携带数据

        for card in cards:
            try:
                note = await extract_card_data(card)
                if note and note.get("id") and note["id"] not in notes_map:
                    title = note.get("title", "")
                    if is_health_related(title):
                        notes_map[note["id"]] = note
                        print(f"  [{len(notes_map):3d}] {title[:40]}…")
                    else:
                        notes_map[note["id"]] = None  # 标记为已见但不相关
            except Exception:
                pass

        cur_count = sum(1 for v in notes_map.values() if v is not None)
        if cur_count >= MAX_NOTES:
            break

        if cur_count == prev_count:
            no_new_rounds += 1
            if no_new_rounds >= 3:
                print("  ⚠️  连续3次无新数据，停止滚动")
                break
        else:
            no_new_rounds = 0
        prev_count = cur_count

        # 滚动到底部
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(SCROLL_PAUSE)
        print(f"  滚动第 {i+1} 次，已收集养生相关 {cur_count} 条…")

    return [v for v in notes_map.values() if v is not None][:MAX_NOTES]


async def extract_card_data(card) -> dict | None:
    """从搜索结果链接中提取笔记 ID 和 URL（详细数据在详情页补充）"""
    note = {}
    try:
        href = await card.get_attribute("href") or ""
        m = re.search(r"/explore/([a-f0-9]+)", href)
        if not m:
            return None
        note["id"] = m.group(1)
        note["note_url"] = "https://www.xiaohongshu.com" + href if href.startswith("/") else href

        # 尝试从链接文字或周围元素取标题
        texts = await page_get_nearby_text(card)
        note["title"] = texts.get("title", "")

        # 封面图
        note["cover_image"] = texts.get("cover", "")
    except Exception:
        return None

    note.setdefault("title", "")
    note.setdefault("reads", 0)
    note.setdefault("likes", 0)
    note.setdefault("saves", 0)
    note.setdefault("comments", 0)
    note.setdefault("shares", 0)
    note.setdefault("followers", 0)
    note.setdefault("publish_date", "")
    note.setdefault("tags", KEYWORD)
    note.setdefault("cover_image", "")
    note.setdefault("blogger_name", "")
    note.setdefault("blogger_avatar", "")
    note.setdefault("blogger_bio", "")
    note.setdefault("note_url", "")
    return note


async def page_get_nearby_text(link_el) -> dict:
    """用 JS 从链接周围提取标题和封面"""
    try:
        result = await link_el.evaluate("""el => {
            const res = {};
            // 找最近含 img 的祖先
            let cur = el;
            for (let i = 0; i < 6 && cur; i++) {
                const img = cur.querySelector('img');
                if (img) {
                    res.cover = img.src || img.dataset.src || '';
                    break;
                }
                cur = cur.parentElement;
            }
            // 找文字
            cur = el;
            for (let i = 0; i < 6 && cur; i++) {
                const spans = cur.querySelectorAll('span, p, div');
                for (const s of spans) {
                    const t = s.innerText?.trim();
                    if (t && t.length > 4 && t.length < 200 && !t.includes('\\n')) {
                        res.title = t;
                        break;
                    }
                }
                if (res.title) break;
                cur = cur.parentElement;
            }
            return res;
        }""")
        return result or {}
    except Exception:
        return {}


async def enrich_note(page: Page, note: dict) -> dict:
    """访问笔记详情页，补充完整数据（阅读量、收藏、评论、博主粉丝等）"""
    url = note.get("note_url")
    if not url:
        return note
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
        await asyncio.sleep(2)

        async def get_text(selectors):
            for sel in selectors:
                el = await page.query_selector(sel)
                if el:
                    t = (await el.inner_text()).strip()
                    if t:
                        return t
            return ""

        # 标题（详情页更准确）
        title = await get_text(["#detail-title", "div.title", "h1"])
        if title:
            note["title"] = title

        # 点赞
        likes_text = await get_text([
            "[class*='likeCount']", ".like-wrapper .count",
            "span[class*='like'] span", ".interact-container span:nth-child(1)"
        ])
        if likes_text:
            note["likes"] = parse_num(likes_text)

        # 收藏
        saves_text = await get_text([
            "[class*='collectCount']", ".collect-wrapper .count",
            "span[class*='collect'] span"
        ])
        if saves_text:
            note["saves"] = parse_num(saves_text)

        # 评论
        comments_text = await get_text([
            "[class*='commentCount']", ".comment-wrapper .count",
            ".comments-container .total", "[class*='comment'] span"
        ])
        if comments_text:
            note["comments"] = parse_num(comments_text)

        # 分享（部分页面有）
        shares_text = await get_text(["[class*='shareCount']", ".share-wrapper .count"])
        if shares_text:
            note["shares"] = parse_num(shares_text)

        # 发布时间
        date_text = await get_text([
            "[class*='date']", "span.date", ".bottom-container span",
            "[class*='time']"
        ])
        if date_text:
            note["publish_date"] = date_text.replace("编辑于", "").strip()

        # 博主粉丝数
        fans_text = await get_text([
            "[class*='fansCount']", ".user-fans span",
            ".fans-wrapper span", "[class*='fans']"
        ])
        if fans_text:
            note["followers"] = parse_num(fans_text)

        # 博主简介
        bio_text = await get_text(["[class*='description']", ".user-desc", ".bio"])
        if bio_text:
            note["blogger_bio"] = bio_text[:100]

    except Exception as e:
        print(f"    ⚠️  详情页解析失败 ({note.get('id')}): {e}")

    return note


async def main():
    print("=" * 55)
    print("  🌿 小红书养生赛道爬虫")
    print(f"  搜索关键词：{KEYWORD}")
    print(f"  目标笔记数：{MAX_NOTES}")
    print("=" * 55)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,  # 保持可见，便于登录和调试
            args=["--window-size=1280,800"]
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )

        page = await context.new_page()

        # ── 步骤 1：打开小红书
        print("\n📡 正在打开小红书…")
        await page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # ── 步骤 2：尝试加载 Cookies
        has_cookies = await load_cookies(context)
        if has_cookies:
            await page.reload(wait_until="domcontentloaded")
            await asyncio.sleep(3)

        # ── 步骤 3：检查登录状态
        logged_in = await check_login(page)
        if not logged_in:
            await wait_for_login(page, context)

        # ── 步骤 4：搜索养生内容
        encoded = urllib.parse.quote(KEYWORD)
        search_url = f"https://www.xiaohongshu.com/search_result?keyword={encoded}&type=51"
        print(f"\n🔍 搜索：{KEYWORD}")
        await page.goto(search_url, wait_until="domcontentloaded")
        await asyncio.sleep(5)  # 等待 JS 渲染

        # 检查是否再次弹出登录框
        if await is_login_modal_visible(page):
            print("  ⚠️  需要重新登录，请扫码…")
            await wait_for_login(page, context)
            await page.goto(search_url, wait_until="domcontentloaded")
            await asyncio.sleep(5)

        # 若内容区还是空，尝试用搜索框
        links = await page.query_selector_all("a[href*='/explore/']")
        if not links:
            print("  ⚠️  内容未加载，尝试使用搜索框…")
            for sel in ["input[placeholder*='搜索']", "input[type='search']", ".search-input input"]:
                box = await page.query_selector(sel)
                if box:
                    await box.click()
                    await page.keyboard.press("Control+A")
                    await box.type(KEYWORD)
                    await page.keyboard.press("Enter")
                    await asyncio.sleep(5)
                    break

        # ── 步骤 5：滚动收集卡片
        print(f"\n📋 开始采集笔记列表…")
        notes = await scroll_and_collect(page)
        print(f"\n  共找到 {len(notes)} 条笔记，开始补充详情…\n")

        # ── 步骤 6：逐条访问详情页补充数据
        enriched = []
        for idx, note in enumerate(notes, 1):
            print(f"  [{idx}/{len(notes)}] 补充详情：{note.get('title', '')[:35]}…")
            note = await enrich_note(page, note)
            enriched.append(note)
            await asyncio.sleep(1.5)  # 礼貌性延迟，避免触发限流

        # ── 步骤 7：保存数据
        output = {
            "keyword": KEYWORD,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "notes": enriched,
        }
        Path(OUTPUT_FILE).write_text(
            json.dumps(output, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"\n✅ 已保存 {len(enriched)} 条笔记到 {OUTPUT_FILE}")
        print("   刷新浏览器看板页面即可查看最新数据！\n")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
