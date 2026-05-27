#!/usr/bin/env python3
"""
每日热点新闻聚合 - 抓取微博/知乎/百度/GitHub 热榜，推送到飞书机器人。
"""

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import requests

# 修复 Windows 终端 GBK 编码问题
if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def get_webhook_url():
    url = os.environ.get("FEISHU_WEBHOOK_URL", "")
    if not url:
        config = load_config()
        url = config.get("webhook_url", "")
    if not url:
        print("[ERROR] 未配置飞书 Webhook 地址。")
        print("请在 config.json 中设置 webhook_url，或设置环境变量 FEISHU_WEBHOOK_URL")
        sys.exit(1)
    return url


# ─── 新闻源 ───────────────────────────────────────────────────────

def fetch_weibo():
    """微博热搜 - hot_band 接口"""
    try:
        resp = requests.get(
            "https://weibo.com/ajax/statuses/hot_band",
            headers={"User-Agent": UA, "Referer": "https://weibo.com/"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        items = []
        for band in data.get("data", {}).get("band_list", [])[:15]:
            word = band.get("word", "")
            if word:
                raw_hot = band.get("raw_hot", "")
                if raw_hot:
                    raw_hot = str(raw_hot)
                items.append({
                    "title": word,
                    "url": f"https://s.weibo.com/weibo?q={word}",
                    "hot": raw_hot,
                })
        return "🔥 微博热搜", items
    except Exception as e:
        print(f"[微博] 获取失败: {e}")
        return "🔥 微博热搜", []


def fetch_zhihu():
    """知乎热榜 - top_search 接口"""
    try:
        resp = requests.get(
            "https://www.zhihu.com/api/v4/search/top_search",
            headers={"User-Agent": UA},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        items = []
        for word in data.get("top_search", {}).get("words", [])[:15]:
            query = word.get("display_query") or word.get("query", "")
            if query:
                items.append({
                    "title": query,
                    "url": f"https://www.zhihu.com/search?type=content&q={query}",
                })
        return "💡 知乎热榜", items
    except Exception as e:
        print(f"[知乎] 获取失败: {e}")
        return "💡 知乎热榜", []


def fetch_baidu():
    """百度热搜"""
    try:
        resp = requests.get(
            "https://top.baidu.com/api/board?platform=wise&tab=realtime",
            headers={"User-Agent": UA},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        items = []
        cards = data.get("data", {}).get("cards", [])
        for card in cards:
            for content_block in card.get("content", []):
                for item in content_block.get("content", [])[:15]:
                    word = item.get("word", "")
                    url = item.get("url", "")
                    hot_tag = item.get("hotTag", "")
                    if word:
                        items.append({
                            "title": word,
                            "url": url,
                            "hot": hot_tag,
                        })
        # 去重（百度接口可能返回重复项）
        seen = set()
        unique = []
        for it in items:
            if it["title"] not in seen:
                seen.add(it["title"])
                unique.append(it)
        return "🔴 百度热搜", unique[:15]
    except Exception as e:
        print(f"[百度] 获取失败: {e}")
        return "🔴 百度热搜", []


def fetch_github():
    """GitHub 近期高星项目（科技风向标），过滤可疑仓库"""
    try:
        since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        resp = requests.get(
            "https://api.github.com/search/repositories",
            params={
                "sort": "stars",
                "order": "desc",
                "q": f"stars:>100 created:>={since}",
                "per_page": 20,
            },
            headers={"User-Agent": UA, "Accept": "application/vnd.github.v3+json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        items = []
        for repo in data.get("items", []):
            full_name = repo.get("full_name", "")
            owner = full_name.split("/")[0] if "/" in full_name else ""
            # 过滤疑似机器账号：用户名包含长数字串
            if re.search(r"\d{6,}", owner):
                continue
            desc = repo.get("description", "") or ""
            if len(desc) > 60:
                desc = desc[:60] + "..."
            items.append({
                "title": f"{full_name}  ⭐{repo['stargazers_count']}",
                "url": repo["html_url"],
                "desc": desc,
            })
            if len(items) >= 10:
                break
        return "🐙 GitHub Trending", items
    except Exception as e:
        print(f"[GitHub] 获取失败: {e}")
        return "🐙 GitHub Trending", []


def fetch_hackernews():
    """Hacker News 当日热门（英文技术新闻）"""
    try:
        # 先获取 top stories IDs
        resp = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json",
            headers={"User-Agent": UA},
            timeout=10,
        )
        resp.raise_for_status()
        ids = resp.json()[:15]

        # 并发获取每个 story 详情
        def get_story(sid):
            try:
                r = requests.get(
                    f"https://hacker-news.firebaseio.com/v0/item/{sid}.json",
                    headers={"User-Agent": UA},
                    timeout=5,
                )
                return r.json()
            except Exception:
                return None

        items = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(get_story, sid): sid for sid in ids}
            for future in as_completed(futures):
                story = future.result()
                if story and story.get("title"):
                    url = story.get("url") or f"https://news.ycombinator.com/item?id={story['id']}"
                    items.append({
                        "title": story["title"],
                        "url": url,
                        "hot": str(story.get("score", 0)),
                    })
                if len(items) >= 10:
                    break

        # 按分数排序
        items.sort(key=lambda x: int(x.get("hot", 0)), reverse=True)
        return "🧡 Hacker News", items[:10]
    except Exception as e:
        print(f"[HackerNews] 获取失败: {e}")
        return "🧡 Hacker News", []


# ─── 摘要提取 ────────────────────────────────────────────────────

def extract_summary(url):
    """从页面 meta 标签提取描述作为摘要"""
    try:
        resp = requests.get(
            url, headers={"User-Agent": UA}, timeout=4, allow_redirects=True
        )
        html = resp.text[:80000]
        # og:description
        m = re.search(
            r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"', html
        )
        if m:
            return m.group(1)[:120]
        # meta description
        m = re.search(
            r'<meta[^>]+name="description"[^>]+content="([^"]+)"', html
        )
        if m:
            return m.group(1)[:120]
        # 兜底：第一个有意义的 <p>
        for m in re.finditer(r"<p[^>]*>(.*?)</p>", html, re.DOTALL):
            text = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            if len(text) > 30:
                return text[:120]
    except Exception:
        pass
    return ""


def enrich_summaries(results):
    """为缺少描述的条目并发抓取页面摘要（HN 文章链接为主）"""
    tasks = []
    for source_name, items in results:
        for item in items:
            # 跳过已有描述或摘要的
            if item.get("desc") or item.get("summary"):
                continue
            url = item.get("url", "")
            if url:
                tasks.append((url, item))

    if not tasks:
        return

    print(f"  抓取 {len(tasks)} 条摘要...")

    def do_fetch(url, item):
        return item, extract_summary(url)

    count = 0
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(do_fetch, u, i) for u, i in tasks]
        for future in as_completed(futures):
            item, summary = future.result()
            if summary:
                item["summary"] = summary
                count += 1

    print(f"  获取到 {count} 条摘要")


# ─── 翻译 ────────────────────────────────────────────────────────

def is_english(text):
    """检测文本是否主要为英文（需要翻译）"""
    if not text:
        return False
    alpha = sum(1 for c in text if c.isascii() and c.isalpha())
    return alpha > len(text) * 0.5


def translate_single(text):
    """翻译单条文本（MyMemory 免费 API）"""
    try:
        resp = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text, "langpair": "en|zh"},
            headers={"User-Agent": UA},
            timeout=5,
        )
        data = resp.json()
        translated = data.get("responseData", {}).get("translatedText", "")
        if translated and translated != text:
            return translated
    except Exception:
        pass
    return ""


def translate_news(results):
    """翻译结果中的英文内容（标题 + 描述 + 摘要）"""
    tasks = []
    for source_name, items in results:
        if source_name == "🧡 Hacker News":
            for item in items:
                if is_english(item["title"]):
                    tasks.append(("hn_title", item))
                if item.get("summary") and is_english(item["summary"]):
                    tasks.append(("hn_summary", item))
        elif source_name == "🐙 GitHub Trending":
            for item in items:
                if item.get("desc") and is_english(item["desc"]):
                    tasks.append(("gh_desc", item))

    if not tasks:
        return

    def do_translate(task_type, item):
        if task_type == "hn_title":
            text = item["title"]
        elif task_type == "hn_summary":
            text = item["summary"]
        else:
            text = item["desc"]
        zh = translate_single(text)
        return task_type, item, zh

    print(f"  翻译 {len(tasks)} 条英文内容...")
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(do_translate, t, i) for t, i in tasks]
        for future in as_completed(futures):
            task_type, item, zh = future.result()
            if zh:
                if task_type == "hn_title":
                    item["title_zh"] = zh
                elif task_type == "hn_summary":
                    item["summary_zh"] = zh
                else:
                    item["desc_zh"] = zh


# ─── 飞书消息 ─────────────────────────────────────────────────────

def build_card(results, source_config):
    """构建飞书交互式卡片"""
    today = datetime.now().strftime("%Y年%m月%d日")
    enabled = set(source_config)

    key_map = {
        "🔥 微博热搜": "weibo",
        "💡 知乎热榜": "zhihu",
        "🔴 百度热搜": "baidu",
        "🐙 GitHub Trending": "github",
        "🧡 Hacker News": "hackernews",
    }

    elements = []
    for source_name, items in results:
        if not items:
            continue
        key = key_map.get(source_name, "")
        if key not in enabled:
            continue

        lines = []
        is_hn = source_name == "🧡 Hacker News"
        is_gh = source_name == "🐙 GitHub Trending"

        for i, item in enumerate(items, 1):
            title = item["title"].replace("[", "\\[").replace("]", "\\]")
            url = item.get("url", "")
            hot = item.get("hot", "")
            desc = item.get("desc", "")
            summary = item.get("summary", "")
            title_zh = item.get("title_zh", "")
            desc_zh = item.get("desc_zh", "")
            summary_zh = item.get("summary_zh", "")

            parts = []
            if url:
                parts.append(f"{i}. [{title}]({url})")
            else:
                parts.append(f"{i}. {title}")
            if hot:
                parts.append(f"  `{hot}`")
            if desc:
                parts.append(f"  — {desc}")

            lines.append("".join(parts))

            # 翻译标题
            if is_hn and title_zh:
                lines.append(f"     ↳ {title_zh}")

            # 摘要（优先中文，否则英文）
            if summary_zh:
                lines.append(f"     > {summary_zh}")
            elif summary:
                lines.append(f"     > {summary}")

            # 翻译描述
            if is_gh and desc_zh:
                lines.append(f"     ↳ {desc_zh}")

        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**{source_name}**\n" + "\n".join(lines),
            },
        })
        elements.append({"tag": "hr"})

    if elements:
        elements.pop()

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📰 每日热点日报 | {today}"},
                "template": "blue",
            },
            "elements": elements,
        },
    }
    return card


def send_to_feishu(webhook_url, card):
    """通过 Webhook 发送卡片消息到飞书"""
    # 打印 webhook URL（脱敏）
    masked = webhook_url[:40] + "***" + webhook_url[-10:] if len(webhook_url) > 50 else webhook_url
    print(f"[DEBUG] Webhook URL: {masked}")
    print(f"[DEBUG] Card size: {len(json.dumps(card, ensure_ascii=False))} chars")
    resp = requests.post(webhook_url, json=card, timeout=15)
    print(f"[DEBUG] HTTP status: {resp.status_code}")
    print(f"[DEBUG] Response body: {resp.text[:500]}")
    result = resp.json()
    code = result.get("code", -1)
    if code == 0:
        print("✅ 已成功推送到飞书！")
    else:
        print(f"❌ 飞书返回错误: code={code}, msg={result.get('msg', '')}")
    return code == 0


# ─── 入口 ─────────────────────────────────────────────────────────

def main():
    print(f"⏰ 开始抓取新闻... {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    config = load_config()
    source_config = config.get("sources", ["weibo", "zhihu", "baidu", "github", "hackernews"])

    fetchers = []
    if "weibo" in source_config:
        fetchers.append(fetch_weibo)
    if "zhihu" in source_config:
        fetchers.append(fetch_zhihu)
    if "baidu" in source_config:
        fetchers.append(fetch_baidu)
    if "github" in source_config:
        fetchers.append(fetch_github)
    if "hackernews" in source_config:
        fetchers.append(fetch_hackernews)

    results = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fn): fn.__name__ for fn in fetchers}
        for future in as_completed(futures):
            name, items = future.result()
            results.append((name, items))
            print(f"  {name}: {len(items)} 条")

    total = sum(len(items) for _, items in results)
    print(f"\n📊 共获取 {total} 条新闻\n")

    enrich_summaries(results)
    translate_news(results)

    webhook_url = get_webhook_url()
    card = build_card(results, source_config)
    send_to_feishu(webhook_url, card)


if __name__ == "__main__":
    main()
