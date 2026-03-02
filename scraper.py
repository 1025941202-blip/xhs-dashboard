"""
小红书养生赛道爬虫 v4（精准拦截 search/notes 接口）
"""
from __future__ import annotations

import asyncio
import json
import urllib.parse
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Response

KEYWORDS    = ["养生", "中医养生", "食疗养生"]
MAX_NOTES   = 100
OUTPUT_FILE = "data.json"
COOKIES_FILE= "cookies.json"


def to_int(v) -> int:
    try:
        s = str(v).strip().replace(",", "")
        if "万" in s:
            return int(float(s.replace("万", "")) * 10000)
        return int(float(s))
    except Exception:
        return 0


def fix_url(url: str) -> str:
    if url and not url.startswith("http"):
        return "https:" + url
    return url or ""


def parse_item(item: dict) -> dict | None:
    """解析 search/notes 接口返回的单条笔记"""
    try:
        note_id = item.get("id") or item.get("note_id") or ""
        if not note_id:
            return None

        card = item.get("note_card") or {}

        # 标题
        title = (card.get("display_title") or card.get("title") or "").strip()

        # 封面
        cover_obj = card.get("cover") or {}
        cover = fix_url(cover_obj.get("url_default") or cover_obj.get("url") or "")

        # 互动数据
        ia = card.get("interact_info") or {}
        likes    = to_int(ia.get("liked_count")     or ia.get("like_count")     or 0)
        saves    = to_int(ia.get("collected_count") or ia.get("collect_count")  or 0)
        comments = to_int(ia.get("comment_count")   or 0)
        shares   = to_int(ia.get("shared_count")    or ia.get("share_count")    or 0)

        # 发布时间（从 corner_tag_info 取）
        pub_date = ""
        for tag in card.get("corner_tag_info") or []:
            if isinstance(tag, dict) and tag.get("type") == "publish_time":
                pub_date = tag.get("text") or ""
                break

        # 博主信息
        user = card.get("user") or {}
        blogger_name   = user.get("nickname") or user.get("nick_name") or user.get("name") or ""
        blogger_avatar = fix_url(user.get("avatar") or user.get("image") or "")
        blogger_bio    = (user.get("desc") or "")[:100]
        followers      = to_int(user.get("fans") or user.get("fans_count") or 0)

        # 标签
        tags_raw = card.get("tag_list") or []
        tags = ",".join(t.get("name", "") for t in tags_raw[:3] if isinstance(t, dict))

        return {
            "id": note_id,
            "title": title,
            "cover_image": cover,
            "reads": 0,
            "likes": likes,
            "saves": saves,
            "comments": comments,
            "shares": shares,
            "publish_date": pub_date,
            "tags": tags,
            "blogger_name": blogger_name,
            "blogger_avatar": blogger_avatar,
            "blogger_bio": blogger_bio,
            "followers": followers,
            "note_url": f"https://www.xiaohongshu.com/explore/{note_id}",
        }
    except Exception as e:
        return None


async def main():
    print("=" * 55)
    print("  🌿 小红书养生赛道爬虫 v4")
    print(f"  关键词：{' / '.join(KEYWORDS)}")
    print("=" * 55)

    collected: dict[str, dict] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, args=["--window-size=1280,900"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )

        # 加载已保存的 cookies
        cp = Path(COOKIES_FILE)
        if cp.exists():
            await context.add_cookies(json.loads(cp.read_text()))
            print("\n  ✅ 已加载本地 Cookies")

        page = await context.new_page()

        # ── 拦截 search/notes 接口
        async def on_response(resp: Response):
            if "search/notes" not in resp.url:
                return
            if resp.status != 200:
                return
            try:
                body = await resp.json()
                # 兼容两种结构：{data:{items:[]}} 和 {code:0, data:{items:[]}}
                data = body.get("data") or body
                items = data.get("items") or []
                new_count = 0
                for item in items:
                    if item.get("model_type") != "note":
                        continue
                    note = parse_item(item)
                    if note and note["id"] not in collected and note.get("title"):
                        collected[note["id"]] = note
                        new_count += 1
                        print(f"  [{len(collected):3d}] ❤️ {note['likes']:>6} ⭐{note['saves']:>6} | {note['title'][:38]}")
                if new_count:
                    print(f"        └─ 本次新增 {new_count} 条，共 {len(collected)} 条")
            except Exception as e:
                print(f"  ⚠️  解析失败: {e}")

        page.on("response", on_response)

        # ── 打开主页 + 登录检测
        print("\n📡 打开小红书...")
        await page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        try:
            m = await page.query_selector("text=手机号登录")
            if m and await m.is_visible():
                print("\n🔐 请在浏览器中扫码登录小红书...")
                print("   登录完成后脚本自动继续（最多等 3 分钟）\n")
                for _ in range(180):
                    await asyncio.sleep(1)
                    try:
                        m2 = await page.query_selector("text=手机号登录")
                        if not m2 or not await m2.is_visible():
                            break
                    except Exception:
                        break
                await asyncio.sleep(3)
                raw = await context.cookies()
                cp.write_text(json.dumps(raw, ensure_ascii=False, indent=2))
                print("  ✅ 登录成功，Cookies 已保存")
        except Exception:
            pass

        # ── 逐关键词搜索
        for keyword in KEYWORDS:
            if len(collected) >= MAX_NOTES:
                break
            encoded = urllib.parse.quote(keyword)
            url = f"https://www.xiaohongshu.com/search_result?keyword={encoded}&type=51"
            print(f"\n🔍 搜索：{keyword}")
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(4)

            no_new = 0
            prev = len(collected)
            for i in range(30):
                if len(collected) >= MAX_NOTES:
                    break
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2.5)
                cur = len(collected)
                if cur == prev:
                    no_new += 1
                    if no_new >= 4:
                        print(f"  ↳ 无新数据，切换下一关键词")
                        break
                else:
                    no_new = 0
                prev = cur

        print(f"\n📊 共收集 {len(collected)} 条养生笔记")
        await browser.close()

    # ── 保存
    notes = list(collected.values())[:MAX_NOTES]
    output = {
        "keyword": " / ".join(KEYWORDS),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "notes": notes,
    }
    Path(OUTPUT_FILE).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 已保存 {len(notes)} 条笔记到 {OUTPUT_FILE}\n")


if __name__ == "__main__":
    asyncio.run(main())
