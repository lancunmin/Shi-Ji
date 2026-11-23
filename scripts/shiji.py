#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shiji (拾迹) — 中文互联网公开信息检索小工具，零第三方依赖。

它只做三件在反爬环境下仍然成立的事：
  1. 用服务端仍能取到结果的中文搜索引擎检索（360 / Bing CN / DuckDuckGo）；
  2. 走搜狗微信拿到公众号文章，并把 /link 解成真实 mp.weixin.qq.com 地址；
  3. 用 B站公开接口把昵称变成 mid、把 mid 变成简介/粉丝数/代表作。

用法示例：
  python shiji.py doctor
  python shiji.py search "罗翔 中国政法大学" --engine 360,bing --limit 8
  python shiji.py wx "罗翔 刑法" --resolve 2
  python shiji.py bili-user "罗翔"
  python shiji.py bili-space 517327498
  python shiji.py fetch https://mp.weixin.qq.com/s/xxxx
  python shiji.py variants "雾港信箱"
  python shiji.py ledger add --claim "..." --verdict confirmed --url ... --quote "..."
  python shiji.py ledger render --out report.md

设计约束（改代码时请保持）：
  * 零依赖：只用标准库，别人 git clone 下来就能跑。
  * 克制的请求量：默认每域名 1.2s 间隔、单次运行有预算，不做批量爬取。
  * 失败要说人话：被风控时抛出明确的下一步建议，而不是抛 traceback。
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.1.0"
UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "shiji")
CACHE_TTL = 20 * 60  # 20 分钟内同一个 URL 不重复请求

# 各引擎被风控后的冷却时长（秒）。国内搜索引擎对同一出口的自动化查询很敏感，
# 撞一次验证码就进冷却，比反复重试更快——重试只会把封禁时间拉长。
COOLDOWN = {"360": 900, "weixin": 600, "bing": 180, "ddg": 180}
COOLDOWN_FILE = os.path.join(CACHE_DIR, "cooldown.json")

# 全局请求预算：防止一次调查把站点打穿，也防止自己卡死
BUDGET = {"requests": 0, "max": 120}
MIN_INTERVAL = {}  # host -> last request ts


class Blocked(Exception):
    """站点返回了风控/验证页，说明这条路当前不通。"""


class BudgetExceeded(Exception):
    pass


def _load_cooldowns():
    try:
        with open(COOLDOWN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {k: float(v) for k, v in data.items() if float(v) > time.time()}
    except (OSError, ValueError):
        return {}


def set_cooldown(name, seconds=None):
    seconds = seconds or COOLDOWN.get(name, 300)
    data = _load_cooldowns()
    data[name] = time.time() + seconds
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(COOLDOWN_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass
    return seconds


def cooldown_left(name):
    left = _load_cooldowns().get(name, 0) - time.time()
    return max(0, int(left))


# ---------------------------------------------------------------- HTTP 基础

def _ssl_ctx():
    ctx = ssl.create_default_context()
    return ctx


def _cache_path(url):
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", url)[-120:]
    return os.path.join(CACHE_DIR, safe + ".txt")


def _cache_get(url):
    p = _cache_path(url)
    if not os.path.exists(p):
        return None
    if time.time() - os.path.getmtime(p) > CACHE_TTL:
        return None
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _cache_put(url, text):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_path(url), "w", encoding="utf-8", errors="replace") as f:
            f.write(text)
    except OSError:
        pass


class Session:
    """一个站点一个会话，带 cookie、限速、重试与缓存。"""

    def __init__(self, name, min_interval=1.2, ua=UA_DESKTOP, use_cache=True):
        self.name = name
        self.min_interval = min_interval
        self.ua = ua
        self.use_cache = use_cache
        self.jar = http.cookiejar.CookieJar()
        ctx = _ssl_ctx()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=ctx),
        )

    def _throttle(self):
        last = MIN_INTERVAL.get(self.name, 0)
        wait = self.min_interval - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        MIN_INTERVAL[self.name] = time.time()

    def get(self, url, headers=None, timeout=12, retries=1, allow_cache=True):
        if self.use_cache and allow_cache:
            hit = _cache_get(url)
            if hit is not None:
                return hit
        left = cooldown_left(self.name)
        if left > 0:
            raise Blocked(f"{self.name} 冷却中（还剩 {left // 60} 分 {left % 60} 秒），换别的源。")
        if BUDGET["requests"] >= BUDGET["max"]:
            raise BudgetExceeded(
                f"本次调查已用满 {BUDGET['max']} 次请求预算。"
                "先用已有结果下判断，或分多轮再来。")
        last_err = None
        for attempt in range(retries + 1):
            self._throttle()
            BUDGET["requests"] += 1
            req = urllib.request.Request(url, headers={
                "User-Agent": self.ua,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                **(headers or {}),
            })
            try:
                with self.opener.open(req, timeout=timeout) as r:
                    raw = r.read(900000)
                    ctype = r.headers.get("content-type", "")
                    charset = "utf-8"
                    m = re.search(r"charset=([\w-]+)", ctype, re.I)
                    if m:
                        charset = m.group(1)
                    else:
                        m2 = re.search(rb'charset=["\']?([\w-]+)', raw[:2048], re.I)
                        if m2:
                            charset = m2.group(1).decode("ascii", "ignore")
                    text = raw.decode(charset, "replace")
                    if self._looks_blocked(text):
                        wait = set_cooldown(self.name)
                        raise Blocked(
                            f"{self.name} 返回了验证/风控页，已冷却 {wait // 60} 分钟。"
                            "换引擎，或等冷却结束/换网络出口。")
                    if self.use_cache and allow_cache:
                        _cache_put(url, text)
                    return text
            except Blocked:
                raise
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}"
                if e.code in (403, 412):
                    wait = set_cooldown(self.name)
                    raise Blocked(
                        f"{self.name} 拒绝访问（HTTP {e.code}），已冷却 {wait // 60} 分钟。")
                if e.code in (400, 404) or attempt >= retries:
                    raise
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = f"{type(e).__name__}: {e}"
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"{self.name} 请求失败：{last_err}")

    @staticmethod
    def _looks_blocked(text):
        head = text[:6000]
        markers = ("百度安全验证", "请输入验证码", "访问过于频繁", "人机验证",
                   "安全验证", "Sina Visitor System", "Just a moment",
                   "环境异常", "拒绝访问")
        return any(m in head for m in markers)


# ---------------------------------------------------------------- 解析工具

def strip_tags(html, keep_breaks=False):
    html = re.sub(r"<(script|style|noscript).*?</\1>", " ", html, flags=re.S | re.I)
    if keep_breaks:
        html = re.sub(r"</(p|div|br|li|h\d)>", "\n", html, flags=re.I)
        html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", "", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
                .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
                .replace("&ldquo;", "").replace("&rdquo;", "").replace("&mdash;", "—")
                .replace("&ensp;", " ").replace("&hellip;", "…"))
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    if keep_breaks:
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text)
    else:
        text = re.sub(r"\s+", " ", text)
    return text.strip()


def absolutize(base, href):
    href = re.sub(r"[\s]+", "", href or "")
    if not href:
        return ""
    return urllib.parse.urljoin(base, href)


def dedupe(items, key=None):
    seen, out = set(), []
    for it in items:
        k = key(it) if key else it.get("url")
        k = (k or "").split("#")[0].rstrip("/")
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


# ---------------------------------------------------------------- 搜索引擎

class Engine360:
    """360 搜索：服务端直出结果，中文覆盖好，是默认主力。"""
    name = "360"
    base = "https://www.so.com/s"

    def __init__(self):
        self.s = Session("360", min_interval=1.2)

    def search(self, query, limit=10, page=1):
        params = {"q": query}
        if page > 1:
            params["pn"] = str(page)
        url = self.base + "?" + urllib.parse.urlencode(params)
        html = self.s.get(url)
        blocks = re.split(r'<li class="res-list', html)[1:]
        out = []
        for b in blocks:
            b = b.split('<li class="res-list')[0]
            m = re.search(r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
            if not m:
                continue
            desc = re.search(r'<p[^>]*class="[^"]*res-desc[^"]*"[^>]*>(.*?)</p>', b, re.S)
            out.append({
                "title": strip_tags(m.group(2)),
                "url": absolutize(self.base, m.group(1)),
                "snippet": strip_tags(desc.group(1)) if desc else "",
                "engine": self.name,
            })
            if len(out) >= limit:
                break
        return out


class EngineBing:
    """Bing 中国站：对知乎/公众号/英文资料覆盖较好，但对 site: 不严格遵守。"""
    name = "bing"
    base = "https://cn.bing.com/search"

    def __init__(self):
        self.s = Session("bing", min_interval=1.2)

    def search(self, query, limit=10, page=1):
        params = {"q": query, "count": "20"}
        if page > 1:
            params["first"] = str((page - 1) * 10 + 1)
        url = self.base + "?" + urllib.parse.urlencode(params)
        html = self.s.get(url)
        blocks = re.split(r'<li class="b_algo"', html)[1:]
        out = []
        for b in blocks:
            b = b.split('</li>')[0]
            m = re.search(r'<h2[^>]*>\s*<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', b, re.S)
            if not m:
                m = re.search(r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', b, re.S)
            if not m:
                continue
            desc = re.search(r'<p[^>]*>(.*?)</p>', b, re.S)
            out.append({
                "title": strip_tags(m.group(2)),
                "url": absolutize(self.base, m.group(1)),
                "snippet": strip_tags(desc.group(1)) if desc else "",
                "engine": self.name,
            })
            if len(out) >= limit:
                break
        return out


class EngineDDG:
    """DuckDuckGo html 版：境外/英文资料与不被墙的镜像线索。"""
    name = "ddg"
    base = "https://html.duckduckgo.com/html/"

    def __init__(self):
        self.s = Session("ddg", min_interval=1.5)

    def search(self, query, limit=10, page=1):
        url = self.base + "?" + urllib.parse.urlencode({"q": query})
        html = self.s.get(url)
        out = []
        for m in re.finditer(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
            href = m.group(1)
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            real = qs.get("uddg", [href])[0]
            sn = ""
            out.append({"title": strip_tags(m.group(2)), "url": real, "snippet": sn, "engine": self.name})
        # 摘要跟标题分块，补一次
        snips = re.findall(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', html, re.S)
        for i, s in enumerate(snips):
            if i < len(out):
                out[i]["snippet"] = strip_tags(s)
        return dedupe(out)[:limit]


class SogouWeixin:
    """搜狗微信：公众号文章是中文互联网信息密度最高的信源之一。"""
    name = "weixin"
    base = "https://weixin.sogou.com/weixin"

    def __init__(self):
        self.s = Session("weixin", min_interval=2.0)

    def search(self, query, limit=10, kind=2):
        """kind: 2=文章, 1=公众号"""
        url = self.base + "?" + urllib.parse.urlencode({"type": kind, "query": query})
        html = self.s.get(url)
        if "请输入验证码" in html or "antispider" in html:
            wait = set_cooldown("weixin")
            raise Blocked(f"搜狗微信触发验证码，已冷却 {wait // 60} 分钟。"
                          "这期间用 bing/360 搜「关键词+公众号名」，或等冷却结束。")
        items = []
        # 每条结果包在 <li id="sogou_vr_11002601_box_N"> 里，标题在 h3，摘要在 p.txt-info，
        # 公众号在 span.all-time-y2，时间戳写在 timeConvert('...') 里。
        blocks = re.split(r'<li id="sogou_vr_[^"]*box_', html)[1:]
        for b in blocks:
            m = re.search(r'<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
            if not m:
                continue
            href = absolutize("https://weixin.sogou.com", m.group(1).replace("&amp;", "&"))
            desc = re.search(r'<p class="txt-info"[^>]*>(.*?)</p>', b, re.S)
            acc = re.search(r'class="all-time-y2"[^>]*>(.*?)</span>', b, re.S)
            ts = re.search(r"timeConvert\('(\d+)'\)", b)
            items.append({
                "title": strip_tags(m.group(2)),
                "url": href,
                "snippet": strip_tags(desc.group(1)) if desc else "",
                "account": strip_tags(acc.group(1)) if acc else "",
                "date": time.strftime("%Y-%m-%d", time.localtime(int(ts.group(1)))) if ts else "",
                "engine": self.name,
                "real_url": "" if "/link?url=" in href else href,
            })
            if len(items) >= limit:
                break
        if not items:
            raise RuntimeError("搜狗微信没有解析到结果；页面结构可能变了，或该词太宽泛。")
        return items

    def resolve(self, sogou_url):
        """搜狗 /link 页用 JS 拼真实地址，这里把拼接片段还原。"""
        html = self.s.get(sogou_url, headers={"Referer": "https://weixin.sogou.com/weixin"})
        frags = re.findall(r"url \+= '([^']*)'", html)
        if not frags:
            raise Blocked("该文章链接未解开（可能需要验证码）。可在浏览器里打开搜狗结果页再点进去。")
        real = "".join(frags).replace("@", "")
        return real


class Bili:
    """B站公开接口：昵称→mid→简介/粉丝/代表作，是查人的高价值入口。"""
    name = "bili"

    def __init__(self):
        self.s = Session("bili", min_interval=1.2)

    def _bootstrap(self):
        # 不带 buvid3 直接打搜索接口会被 412
        if not any(c.name == "buvid3" for c in self.s.jar):
            try:
                self.s.get("https://www.bilibili.com/", allow_cache=False)
            except Exception:
                pass

    def user_search(self, keyword, limit=10):
        self._bootstrap()
        url = ("https://api.bilibili.com/x/web-interface/search/type?"
               + urllib.parse.urlencode({"search_type": "bili_user", "keyword": keyword}))
        body = self.s.get(url, headers={"Referer": "https://www.bilibili.com/"})
        j = json.loads(body)
        if j.get("code") != 0:
            raise RuntimeError(f"B站用户搜索失败：{j.get('code')} {j.get('message')}")
        out = []
        for it in (j.get("data", {}).get("result") or [])[:limit]:
            out.append({
                "uname": it.get("uname"),
                "mid": it.get("mid"),
                "fans": it.get("fans"),
                "level": it.get("level"),
                "sign": it.get("usign") or it.get("sign") or "",
                "videos": it.get("videos"),
                "url": f"https://space.bilibili.com/{it.get('mid')}",
                "engine": self.name,
            })
        return out

    def video_search(self, keyword, limit=10):
        self._bootstrap()
        url = ("https://api.bilibili.com/x/web-interface/search/type?"
               + urllib.parse.urlencode({"search_type": "video", "keyword": keyword}))
        body = self.s.get(url, headers={"Referer": "https://www.bilibili.com/"})
        j = json.loads(body)
        if j.get("code") != 0:
            raise RuntimeError(f"B站视频搜索失败：{j.get('code')} {j.get('message')}")
        out = []
        for it in (j.get("data", {}).get("result") or [])[:limit]:
            out.append({
                "title": strip_tags(it.get("title", "")),
                "author": it.get("author"),
                "mid": it.get("mid"),
                "bvid": it.get("bvid"),
                "play": it.get("play"),
                "pubdate": it.get("pubdate"),
                "desc": strip_tags(it.get("description", ""))[:120],
                "url": f"https://www.bilibili.com/video/{it.get('bvid')}",
                "engine": self.name,
            })
        return out

    def space(self, mid):
        self._bootstrap()
        url = "https://api.bilibili.com/x/web-interface/card?" + urllib.parse.urlencode({"mid": mid})
        body = self.s.get(url, headers={"Referer": f"https://space.bilibili.com/{mid}"})
        j = json.loads(body)
        if j.get("code") != 0:
            raise RuntimeError(f"B站用户卡片失败：{j.get('code')} {j.get('message')}")
        d = j["data"]
        card = d.get("card", {})
        return {
            "mid": card.get("mid"), "name": card.get("name"),
            "sign": card.get("sign"), "level": card.get("level"),
            "fans": card.get("fans"), "attention": card.get("attention"),
            "archive_count": d.get("archive_count"), "article_count": d.get("article_count"),
            "likes": d.get("like_num"),
            "url": f"https://space.bilibili.com/{mid}",
            "raw": card,
        }


ENGINES = {"360": Engine360, "bing": EngineBing, "ddg": EngineDDG}
ENGINE_ORDER = ("bing", "360", "ddg")


def pick_engines(spec):
    """auto：跳过冷却中的源，最多用两个，优先 Bing（最不容易被封）。

    国内搜索引擎对同一出口的自动化查询很敏感，一轮调查里换引擎比死磕一个更快。
    """
    if spec and spec != "auto":
        return [e.strip() for e in spec.split(",") if e.strip()]
    ready = [e for e in ENGINE_ORDER if cooldown_left(e) == 0]
    if not ready:
        return ["bing"]  # 全在冷却也别直接放弃，让 bing 报错比不查更有信息
    return ready[:2]


def do_search(query, engines="auto", limit=8, json_out=False, page=1, host=None):
    results, errors = [], []
    for name in pick_engines(engines):
        cls = ENGINES.get(name)
        if not cls:
            errors.append((name, "未知引擎"))
            continue
        try:
            results += cls().search(query, limit=limit, page=page)
        except Blocked as e:
            errors.append((name, str(e)))
        except Exception as e:  # noqa: BLE001 - 单引擎失败不该拖垮整次调查
            errors.append((name, f"{type(e).__name__}: {e}"))
    results = dedupe(results)
    if host:
        host = host.lower().lstrip(".")
        kept = [r for r in results if host in urllib.parse.urlparse(r["url"]).netloc.lower()]
        results = kept
    if json_out:
        print(json.dumps({"query": query, "results": results,
                          "errors": [{"engine": n, "error": m} for n, m in errors]},
                         ensure_ascii=False, indent=1))
        return results
    print(f"# 查询：{query}   （命中 {len(results)} 条）")
    for i, r in enumerate(results, 1):
        print(f"\n[{i}] {r['title']}")
        print(f"    {r['url']}")
        if r.get("snippet"):
            print(f"    {r['snippet'][:220]}")
    if host and not results:
        print(f"\n（{host} 下没有命中——这本身就是个信息：该站可能没收录，或者要换关键词/换站内搜索）")
    for n, m in errors:
        print(f"\n! {n} 不可用：{m}", file=sys.stderr)
    return results


# ---------------------------------------------------------------- 正文抽取

def extract_readable(html, url=""):
    """够用就好的正文抽取：标题 + 主体文字，微信文章单独处理。"""
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        title = strip_tags(m.group(1))
    if "mp.weixin.qq.com" in url:
        body = re.search(r'<div[^>]*id="js_content"[^>]*>(.*?)</div>\s*(?:<script|<div class="qr_code)', html, re.S)
        if not body:
            body = re.search(r'<div[^>]*id="js_content"[^>]*>(.*)', html, re.S)
        if body:
            text = strip_tags(body.group(1), keep_breaks=True)
            return title, text
    # 去掉导航/页脚等噪声容器
    cleaned = re.sub(r"<(header|footer|nav|aside)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    cleaned = re.sub(r'<div[^>]*(?:class|id)="[^"]*(?:nav|footer|sidebar|comment|advert|breadcrumb)[^"]*"[^>]*>.*?</div>',
                     " ", cleaned, flags=re.S | re.I)
    text = strip_tags(cleaned, keep_breaks=True)
    # 取信息密度最高的一段
    paras = [p.strip() for p in text.split("\n") if len(p.strip()) > 30]
    if not paras:
        return title, text[:3000]
    return title, "\n".join(paras)


def do_fetch(url, max_chars=4000, json_out=False):
    s = Session(urllib.parse.urlparse(url).netloc, min_interval=1.0)
    if "weixin.sogou.com/link" in url:
        real = SogouWeixin().resolve(url)
        print(f"（已解开真实地址）{real}", file=sys.stderr)
        url = real
    html = s.get(url, headers={"Referer": "https://www.weixin.qq.com/" if "weixin" in url else url})
    title, text = extract_readable(html, url)
    text = text[:max_chars]
    if json_out:
        print(json.dumps({"url": url, "title": title, "text": text}, ensure_ascii=False, indent=1))
    else:
        print(f"# {title}\n# {url}\n")
        print(text)
    return title, text


# ---------------------------------------------------------------- 平台深链

# 这些站点自动抓取会被拦（甚至 robots 明确写 Disallow: /），
# 但它们对「人」是开放的。所以不硬闯，直接给人链接——这才是查人时最稳的一条路。
PLATFORM_LINKS = [
    ("微信文章", "https://weixin.sogou.com/weixin?type=2&query={q}", "公众号是中文最深的正文池"),
    ("知乎", "https://www.zhihu.com/search?type=content&q={q}", "回答/文章/想法；站内搜索要登录"),
    ("微博", "https://s.weibo.com/weibo?q={q}", "实时发言、超话、评论区"),
    ("小红书", "https://www.xiaohongshu.com/search_result?keyword={q}", "生活化痕迹、学校/城市/消费"),
    ("抖音", "https://www.douyin.com/search/{q}", "短视频、同城、评论"),
    ("B站", "https://search.bilibili.com/all?keyword={q}", "UP主、动态、评论区"),
    ("贴吧", "https://tieba.baidu.com/f/search/res?qw={q}", "早期网络痕迹、同校/同城吧"),
    ("豆瓣", "https://www.douban.com/search?q={q}", "书影音标记、小组发言"),
    ("头条", "https://so.toutiao.com/search?keyword={q}", "资讯与自媒体"),
    ("百度学术", "https://xueshu.baidu.com/s?wd={q}", "论文、作者单位、合作者"),
    ("天眼查", "https://www.tianyancha.com/search?key={q}", "公司、任职、股权"),
    ("企查查", "https://www.qcc.com/web/search?key={q}", "同上，交叉验证"),
    ("裁判文书", "https://wenshu.court.gov.cn/", "涉诉记录（需站内检索）"),
    ("执行信息", "https://zxgk.court.gov.cn/", "失信/限高（需站内检索）"),
    ("GitHub", "https://github.com/search?q={q}&type=users", "ID、提交邮箱、所在地"),
    ("网易云音乐", "https://music.163.com/#/search/m/?s={q}&type=100", "歌单/评论里的自述"),
    ("闲鱼", "https://www.goofish.com/search?q={q}", "二手交易里的学校/地址线索"),
]


def do_links(keyword, out="", json_out=False):
    rows = [(name, tpl.format(q=urllib.parse.quote(keyword)), why) for name, tpl, why in PLATFORM_LINKS]
    if json_out:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return rows
    lines = [f"# 手动检索清单：{keyword}", "",
             "这些站自动抓不到（风控/登录墙/robots 明确禁止），但对人是开放的。",
             "逐个打开，把看到的正文/关键信息贴回来，比硬爬稳得多。", ""]
    for name, url, why in rows:
        lines.append(f"- [ ] **{name}** — {why}")
        lines.append(f"      {url}")
    text = "\n".join(lines)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"清单已写出：{out}")
    else:
        print(text)
    return rows


# ---------------------------------------------------------------- 画像聚合

# 昵称画像要做的不是「多查几次」，而是把「一条线索自动变成下一条查询」。
# 这张表就是把人肉 pivot 写成正则：签名里出现「大号：X」就自动去查 X。
PIVOT_PATTERNS = [
    ("大号", r"(?:大号|主号|大號|主帐号|主账号)\s*[:：]?\s*([^\s，,。；;|/、]{2,16})"),
    ("小号", r"(?:小号|备用号|小號)\s*[:：]?\s*([^\s，,。；;|/、]{2,16})"),
    ("QQ群", r"(?:群号|粉丝群|QQ群|q群|企鹅群)\s*[:：]?\s*(\d{6,12})"),
    ("微信", r"(?:微信|vx|VX|v信|WX)\s*[:：]?\s*([A-Za-z][A-Za-z0-9_-]{4,19})"),
    ("邮箱", r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"),
    ("微博", r"(?:微博)\s*[:：]?\s*@?([^\s，,。；;|/、]{2,16})"),
    ("手机", r"(1[3-9]\d{9}|\d{3}\*{2,4}\d{4})"),
    ("公众号", r"(?:公众号)\s*[:：]?\s*([^\s，,。；;|/、]{2,16})"),
]

# 明显不是「一个人的账号」的域名，单独归类，别混进账号矩阵
ORG_HINTS = ("qcc.com", "tianyancha.com", "aiqicha.baidu.com", "gsxt.gov.cn", "creditchina.gov.cn")
NEWS_HINTS = ("sohu.com", "163.com", "sina.com.cn", "qq.com", "ifeng.com", "thepaper.cn",
              "toutiao.com", "baijiahao.baidu.com", "zhihu.com", "douban.com")
PLATFORM_HINTS = ("bilibili.com", "weibo.com", "xiaohongshu.com", "douyin.com", "tieba.baidu.com")


def extract_pivots(text):
    """从一段自述里抽出可继续追的线索。"""
    found = []
    if not text:
        return found
    for kind, pat in PIVOT_PATTERNS:
        for m in re.finditer(pat, text):
            val = m.group(1).strip()
            if kind in ("大号", "小号") and len(val) < 2:
                continue
            if kind == "手机" and val.startswith("1") and len(val) == 11:
                val = val[:3] + "****" + val[-4:]  # 打码，报告里不落全号
            found.append({"kind": kind, "value": val})
    seen, out = set(), []
    for f in found:
        k = (f["kind"], f["value"])
        if k in seen:
            continue
        seen.add(k)
        out.append(f)
    return out


def _engine_results(query, limit=6, engines=None):
    res, errs = [], []
    for name in (engines or pick_engines("auto")):
        cls = ENGINES.get(name)
        if not cls:
            continue
        try:
            res += cls().search(query, limit=limit)
        except Blocked as e:
            errs.append(f"{name}: {e}")
        except Exception as e:  # noqa: BLE001
            errs.append(f"{name}: {type(e).__name__}")
    return dedupe(res), errs


def _classify_url(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    if any(h in host for h in ORG_HINTS):
        return "机构/企业"
    if any(h in host for h in PLATFORM_HINTS):
        return "平台内容"
    if any(h in host for h in NEWS_HINTS):
        return "资讯/社区"
    return "其他"


def _term_score(term, text):
    """整词命中 > 半词命中 > 无关。中文被拆词时，结果里只有单字，这时算噪声。"""
    if not term or not text:
        return 0
    if term in text:
        return 2
    if len(term) >= 2:
        grams = {term[i:i + 2] for i in range(len(term) - 1)}
        return 1 if any(g in text for g in grams) else 0
    return 0


def _drop_noise(nickname, results):
    """丢掉完全没有整词命中的结果——它们通常是被分词拆碎后的字典页/无关页。"""
    kept, dropped = [], 0
    for r in results:
        if _term_score(nickname, (r.get("title") or "") + (r.get("snippet") or "")) >= 2:
            kept.append(r)
        else:
            dropped += 1
    return kept, dropped


def do_dossier(nickname, out="", limit=6, json_out=False):
    """一条命令跑完：账号矩阵 → 自述线索 → 顺着线索再查 → 内容线索 → 卡片。"""
    steps, accounts, related, articles, web, pivots = [], [], [], [], [], []

    # 1. 主账号面
    try:
        accounts = Bili().user_search(nickname, limit=limit)
        steps.append(f"B站昵称检索：{len(accounts)} 个账号")
    except Exception as e:  # noqa: BLE001
        steps.append(f"B站昵称检索失败：{e}")

    # 2. 从签名抽线索
    for a in accounts:
        for p in extract_pivots(a.get("sign", "")):
            p["from"] = f"B站/{a.get('uname')}({a.get('mid')})"
            pivots.append(p)

    # 3. 顺着「大号/小号」再查一层（只查一级，避免发散）
    names = {p["value"] for p in pivots if p["kind"] in ("大号", "小号")}
    for name in list(names)[:3]:
        try:
            hits = Bili().user_search(name, limit=3)
            for h in hits[:2]:
                h["via"] = name
                related.append(h)
            steps.append(f"顺着「{name}」再查：{len(hits)} 个账号")
        except Exception as e:  # noqa: BLE001
            steps.append(f"顺着「{name}」再查失败：{e}")

    # 4. 公众号
    try:
        articles = SogouWeixin().search(nickname, limit=5)
        steps.append(f"公众号检索：{len(articles)} 篇")
    except Exception as e:  # noqa: BLE001
        steps.append(f"公众号检索失败：{e}")

    # 5. 网页（丢掉被分词拆碎的噪声）
    raw_web, errs = _engine_results(f"{nickname} 账号", limit=limit + 4)
    web, dropped = _drop_noise(nickname, raw_web)
    steps.append(f"网页检索：整词命中 {len(web)} 条"
                 + (f"，丢弃拆词噪声 {dropped} 条" if dropped else "")
                 + (f"（{'; '.join(errs)}）" if errs else ""))

    # 6. 合成结论（不靠模型，纯按数据说话）
    summary = []
    if accounts:
        top = max(accounts, key=lambda a: a.get("fans") or 0)
        summary.append(f"B站同昵称账号 {len(accounts)} 个，粉丝最多的是「{top['uname']}」"
                       f"（{top.get('fans', 0)} 粉，mid {top['mid']}）")
        withsign = [a for a in accounts if a.get("sign")]
        if withsign:
            summary.append(f"其中 {len(withsign)} 个号写了签名，可作为下一步入口")
    else:
        summary.append("B站没有同昵称账号——这个昵称可能不是 B站 用户，或含特殊符号")
    if pivots:
        kinds = "、".join(sorted({p["kind"] for p in pivots}))
        summary.append(f"签名里抽出 {len(pivots)} 条可追线索（{kinds}）")
    if related:
        summary.append(f"顺着自述线索又查到 {len(related)} 个账号")
    if articles:
        acc = {}
        for a in articles:
            acc[a.get("account") or "?"] = acc.get(a.get("account") or "?", 0) + 1
        main_acc, main_n = max(acc.items(), key=lambda kv: kv[1])
        if main_n >= 3 and main_acc == nickname:
            summary.append(f"公众号里「{main_acc}」占了 {main_n} 篇，是**同名主体**，暂按不同人处理")
        else:
            summary.append(f"公众号命中 {len(articles)} 篇（主要来自「{main_acc}」）")
    if dropped:
        summary.append(f"网页结果有 {dropped} 条完全不含整个昵称，已按拆词噪声丢弃"
                       "——说明这个昵称在搜索引擎里被拆字了，换平台源更划算")

    if json_out:
        print(json.dumps({"nickname": nickname, "summary": summary, "accounts": accounts,
                          "pivots": pivots, "related": related, "articles": articles,
                          "web": web, "dropped_web": dropped, "steps": steps},
                         ensure_ascii=False, indent=1))
        return

    L = [f"# 昵称画像 · {nickname}", "",
         f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M')}",
         f"- 检索路径：{'；'.join(steps)}", "",
         "## 一眼结论", ""]
    for s in summary:
        L.append(f"- {s}")
    L.append("")

    L += ["## 一、账号矩阵（同昵称，未确认同人）", "",
          "| 昵称 | mid | 粉丝 | 签名摘要 | 链接 |", "| --- | --- | --- | --- | --- |"]
    if accounts:
        for a in accounts:
            sign = (a.get("sign") or "").replace("|", "／").replace("\n", " ")[:40]
            L.append(f"| {a['uname']} | {a['mid']} | {a.get('fans', 0)} | {sign} | {a['url']} |")
    else:
        L.append("| （无） | | | | |")

    L += ["", "## 二、自述线索（从签名里抽出来的，可直接追）", ""]
    if pivots:
        L += ["| 类型 | 值 | 出处 |", "| --- | --- | --- |"]
        for p in pivots:
            L.append(f"| {p['kind']} | `{p['value']}` | {p['from']} |")
    else:
        L.append("（签名里没有可追的线索）")

    if related:
        L += ["", "## 三、顺着线索查到的账号", "",
              "| 昵称 | mid | 粉丝 | 通过 | 链接 |", "| --- | --- | --- | --- | --- |"]
        for r in related:
            L.append(f"| {r['uname']} | {r['mid']} | {r.get('fans', 0)} | {r.get('via', '')} | {r['url']} |")

    L += ["", "## 四、公众号线索", ""]
    if articles:
        for a in articles:
            L.append(f"- **{a['title']}** — {a.get('account') or '?'} · {a.get('date') or '?'}")
            if a.get("snippet"):
                L.append(f"  > {a['snippet'][:120]}")
            L.append(f"  {a.get('real_url') or a['url']}")
    else:
        L.append("（无）")

    L += ["", f"## 五、网页线索（整词命中 {len(web)} 条，丢弃噪声 {dropped} 条）", ""]
    if web:
        for w in web:
            L.append(f"- [{_classify_url(w['url'])}] {w['title']} — {urllib.parse.urlparse(w['url']).netloc}")
            L.append(f"  {w['url']}")
    elif dropped:
        L.append("（全部被判定为拆词噪声：结果里连完整昵称都不含，说明搜索引擎把这个词拆碎了——"
                 "改用公众号/B站这类平台源）")
    else:
        L.append("（无）")

    L += ["", "## 六、下一步（按优先级）", ""]
    todo = []
    for p in pivots:
        if p["kind"] == "QQ群":
            todo.append(f"在 QQ 里搜群 `{p['value']}`，看群名/简介/群主（脚本进不去，需登录）")
        elif p["kind"] in ("大号", "小号"):
            todo.append(f"核对 `{p['value']}` 与目标账号的投稿主题、关注列表是否一致")
        elif p["kind"] == "邮箱":
            todo.append(f"用 `{p['value']}` 搜 GitHub 提交记录、论文通讯作者")
        elif p["kind"] == "微信":
            todo.append(f"微信号 `{p['value']}` 直接搜命中率低，改搜「昵称 + 微信」的截图/文章")
    if not todo:
        todo.append("换个带限定词的昵称（加城市/学校/游戏名）再跑一次")
    todo.append("在同一昵称下按「粉丝量 + 签名信息量」排序，优先查信息量最大的那个号")
    for i, t in enumerate(todo, 1):
        L.append(f"{i}. {t}")

    L += ["", "> 本卡片只列公开可访问内容。同名账号不等于同一人；档位与判据见 references/evidence.md。"]
    text = "\n".join(L)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"卡片已写出：{out}")
    else:
        print(text)


# ---------------------------------------------------------------- 平台可达性矩阵

# 关键认知：中文互联网的注册键是手机号，昵称只是展示名。
# 所以「昵称能查到什么」完全取决于平台有没有开放「昵称 → 公开主页」的入口。
# 这张表就是把这件事写死：能查的自动查，查不了的明确标「登录墙 / 风控」，
# 而不是一律报「没注册」——那是错的。
PLATFORMS = [
    # (显示名, 类型, 探针函数名, 主页模板, 昵称能否直接检索)
    {"name": "B站", "kind": "user", "probe": "bili", "home": "https://space.bilibili.com/{id}",
     "note": "免登录接口，昵称精确可搜"},
    {"name": "网易云音乐", "kind": "user", "probe": "netease", "home": "https://music.163.com/#/user/home?id={id}",
     "note": "免登录接口，昵称可搜"},
    {"name": "Gitee", "kind": "user", "probe": "gitee", "home": "https://gitee.com/{id}",
     "note": "开放 API，昵称可搜"},
    {"name": "GitHub", "kind": "user", "probe": "github", "home": "https://github.com/{id}",
     "note": "开放 API，按用户名搜"},
    {"name": "CSDN", "kind": "content", "probe": "csdn", "home": "https://blog.csdn.net/",
     "note": "内容搜索：昵称出现在博客里"},
    {"name": "掘金", "kind": "content", "probe": "juejin", "home": "https://juejin.cn/",
     "note": "内容搜索：昵称出现在文章里"},
    {"name": "豆瓣", "kind": "wall", "probe": "douban", "home": "https://www.douban.com/people/",
     "note": "用户搜索需登录"},
    {"name": "知乎", "kind": "wall", "probe": "zhihu", "home": "https://www.zhihu.com/people/",
     "note": "搜索与主页均 403"},
    {"name": "微博", "kind": "wall", "probe": "weibo", "home": "https://weibo.com/n/",
     "note": "访客系统拦截"},
    {"name": "小红书", "kind": "wall", "probe": "xhs", "home": "https://www.xiaohongshu.com/",
     "note": "JS 渲染 + 登录墙"},
    {"name": "抖音", "kind": "wall", "probe": "douyin", "home": "https://www.douyin.com/",
     "note": "验证码中间页"},
    {"name": "贴吧", "kind": "wall", "probe": "tieba", "home": "https://tieba.baidu.com/home/main?un=",
     "note": "用户页 403"},
    {"name": "淘宝/闲鱼", "kind": "none", "probe": "", "home": "",
     "note": "无公开主页，昵称不可检索"},
    {"name": "微信", "kind": "none", "probe": "", "home": "",
     "note": "微信号/昵称不对外索引"},
    {"name": "QQ", "kind": "none", "probe": "", "home": "",
     "note": "昵称搜索需登录"},
    {"name": "游戏/政务/银行", "kind": "none", "probe": "", "home": "",
     "note": "账号在各自 App 内，无公开入口"},
]


def _probe_bili(nick):
    hits = Bili().user_search(nick, limit=5)
    out = [{"id": h["mid"], "name": h["uname"], "extra": f"{h.get('fans', 0)} 粉 · {(h.get('sign') or '')[:30]}"}
           for h in hits if nick in (h.get("uname") or "")]
    return out, ""


def _probe_netease(nick):
    s = Session("music.163.com", min_interval=1.2)
    url = ("https://music.163.com/api/search/get/web?"
           + urllib.parse.urlencode({"s": nick, "type": 1002, "limit": 10}))
    j = json.loads(s.get(url, headers={"Referer": "https://music.163.com/"}))
    profs = (j.get("result") or {}).get("userprofiles") or []
    out = [{"id": p.get("userId"), "name": p.get("nickname"),
            "extra": f"{(p.get('signature') or '')[:30]} · {p.get('province') or ''}"}
           for p in profs if nick in (p.get("nickname") or "")]
    return out, ""


def _probe_gitee(nick):
    s = Session("gitee.com", min_interval=1.2)
    url = "https://gitee.com/api/v5/search/users?" + urllib.parse.urlencode({"q": nick, "per_page": 10})
    j = json.loads(s.get(url))
    out = [{"id": u.get("login"), "name": u.get("name") or u.get("login"),
            "extra": (u.get("bio") or u.get("location") or "")[:30]}
           for u in j if nick in (u.get("login") or "") or nick in (u.get("name") or "")]
    return out, ""


def _probe_github(nick):
    s = Session("api.github.com", min_interval=1.2)
    url = "https://api.github.com/search/users?" + urllib.parse.urlencode({"q": nick, "per_page": 10})
    j = json.loads(s.get(url, headers={"Accept": "application/vnd.github+json"}))
    out = [{"id": u.get("login"), "name": u.get("login"), "extra": "GitHub 用户"} for u in j.get("items") or []]
    return out, ""


def _probe_csdn(nick):
    s = Session("so.csdn.net", min_interval=1.5)
    url = "https://so.csdn.net/api/v3/search?" + urllib.parse.urlencode({"q": nick, "t": "blog", "p": 1})
    j = json.loads(s.get(url))
    items = []
    if isinstance(j, dict):
        for key in ("result_vos", "data"):
            v = j.get(key)
            if isinstance(v, dict):
                items = v.get("items") or []
            elif isinstance(v, list):
                items = v
            if items:
                break
    out = []
    for it in items[:8]:
        if not isinstance(it, dict):
            continue
        t = re.sub(r"<[^>]+>", "", it.get("title") or "")
        if nick in t or nick in (it.get("description") or ""):
            out.append({"id": it.get("url") or "", "name": t[:40], "extra": "CSDN 博文"})
    return out, ""


def _probe_juejin(nick):
    s = Session("api.juejin.cn", min_interval=1.5)
    url = ("https://api.juejin.cn/search_api/v1/search?"
           + urllib.parse.urlencode({"query": nick, "id_type": 0, "cursor": 0, "limit": 10, "search_type": 0}))
    j = json.loads(s.get(url))
    out = []
    for it in ((j.get("data") or []) if isinstance(j.get("data"), list) else [])[:5]:
        r = it.get("result_model") or {}
        title = r.get("article_info", {}).get("title") or r.get("title") or ""
        if nick in title:
            out.append({"id": r.get("article_id", ""), "name": title[:40], "extra": "掘金文章"})
    return out, ""


WALL_PROBES = {
    "douban": ("https://www.douban.com/search?" + urllib.parse.urlencode({"cat": 1005, "q": "{q}"}), ["登录", "扫码"]),
    "zhihu": ("https://www.zhihu.com/search?" + urllib.parse.urlencode({"type": "people", "q": "{q}"}), ["验证", "登录"]),
    "weibo": ("https://s.weibo.com/user?" + urllib.parse.urlencode({"q": "{q}"}), ["Visitor", "登录"]),
    "xhs": ("https://www.xiaohongshu.com/search_result?" + urllib.parse.urlencode({"keyword": "{q}"}), ["登录", "安全限制"]),
    "douyin": ("https://www.douyin.com/search/{q}", ["验证码", "验证"]),
    "tieba": ("https://tieba.baidu.com/home/main?" + urllib.parse.urlencode({"un": "{q}"}), ["403", "验证"]),
}

PROBES = {"bili": _probe_bili, "netease": _probe_netease, "gitee": _probe_gitee,
          "github": _probe_github, "csdn": _probe_csdn, "juejin": _probe_juejin}


def probe_platform(entry, nick):
    """返回 (状态, 命中列表, 说明)。状态：命中/无/登录墙/风控/不可达。"""
    if entry["kind"] == "none":
        return "无入口", [], entry["note"]
    if entry["kind"] == "wall":
        tpl, marks = WALL_PROBES[entry["probe"]]
        url = tpl.format(q=urllib.parse.quote(nick))
        try:
            html = Session(urllib.parse.urlparse(url).netloc, min_interval=1.2).get(url)
        except Blocked:
            return "风控", [], entry["note"]
        except Exception as e:  # noqa: BLE001
            return "不可达", [], f"{type(e).__name__}"
        if any(m in html[:20000] for m in marks):
            return "登录墙", [], entry["note"]
        if nick not in html:
            # 页面拿到了，但里面根本没有这个昵称——典型的 JS 空壳，不能算「没注册」
            return "不可判定（JS 空壳）", [], entry["note"]
        return "可访问", [], entry["note"]
    # 可脚本化的探针
    fn = PROBES.get(entry["probe"])
    if not fn:
        return "不可达", [], "未实现"
    try:
        hits, note = fn(nick)
    except Blocked:
        return "风控", [], entry["note"]
    except Exception as e:  # noqa: BLE001
        return "不可达", [], f"{type(e).__name__}: {str(e)[:40]}"
    return ("命中" if hits else "无"), hits, entry["note"]


def do_probe(nick, only="", json_out=False):
    rows = []
    for entry in PLATFORMS:
        if only and entry["name"] not in only:
            continue
        status, hits, note = probe_platform(entry, nick)
        rows.append({"platform": entry["name"], "status": status, "hits": hits, "note": note})
    if json_out:
        print(json.dumps({"nickname": nick, "platforms": rows}, ensure_ascii=False, indent=1))
        return rows
    print(f"# 昵称可达性矩阵 · {nick}\n")
    print("| 平台 | 状态 | 命中 | 说明 |")
    print("| --- | --- | --- | --- |")
    for r in rows:
        if r["status"] == "命中":
            detail = "；".join(f"{h['name']}（{h['extra']}）" for h in r["hits"][:3])
        elif r["status"] in ("登录墙", "风控", "不可达", "无入口"):
            detail = "—"
        else:
            detail = "接口通但没这个昵称"
        print(f"| {r['platform']} | {r['status']} | {detail} | {r['note']} |")
    ok = sum(1 for r in rows if r["status"] == "命中")
    wall = sum(1 for r in rows if r["status"] in ("登录墙", "风控"))
    print(f"\n命中 {ok} 个平台；{wall} 个平台查不了（登录墙/风控）。")
    print("**「查不了」不等于「没注册」**——这是昵称检索最大的误判来源。")
    print("要穿透淘宝/游戏/政务这类，只有手机号或邮箱；昵称永远进不去。")
    return rows


# ---------------------------------------------------------------- 标识符矩阵

def do_ids(nickname, handles="", out=""):
    """把一个昵称摊成一组候选标识符——很多人跨站都用变体，单搜一个名字会漏。"""
    ids = []
    seen = set()

    def add(x, why):
        x = (x or "").strip()
        if x and x not in seen:
            seen.add(x)
            ids.append({"id": x, "why": why})

    add(nickname, "原昵称")
    add(nickname.replace(" ", ""), "去空格")
    add(nickname.lower(), "小写")
    for h in [x.strip() for x in handles.split(",") if x.strip()]:
        add(h, "用户提供的其他ID")
    for sep in ("_", "-", "."):
        add(nickname.replace(" ", sep), f"空格换 {sep}")
    for suf in ("1", "2", "123", "2024", "2025", "520"):
        add(nickname + suf, f"加后缀 {suf}")
    if not nickname.isascii():
        add("小" + nickname, "加前缀 小")
        add(nickname + "子", "加后缀 子")
    rows = ids
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(r["id"] for r in rows))
        print(f"标识符清单已写出：{out}")
    else:
        print(f"# 标识符矩阵 · {nickname}（{len(rows)} 个候选）\n")
        for r in rows:
            print(f"  {r['id']:24s} ← {r['why']}")
        print("\n用法：这批 ID 逐个丢给 probe / search；命中率高的是「去空格」和「加后缀」两族。")
    return rows


# ---------------------------------------------------------------- 变体生成

SEPARATORS = ["", "_", "-", ".", "·"]


def variants(handle):
    """把昵称/ID 扩成可检索的变体，覆盖中文互联网常见的起名方式。"""
    h = handle.strip()
    out = []
    seen = set()

    def add(x):
        x = x.strip()
        if x and x not in seen:
            seen.add(x)
            out.append(x)

    add(h)
    add(h.lower())
    add(h.upper() if h.isascii() else h)
    add(h.replace(" ", ""))
    add(h.replace(" ", "_"))
    for sep in SEPARATORS[1:]:
        add(h.replace(" ", sep))
    # 常见后缀：重名、小号、备用号
    for suf in ("1", "2", "123", "2024", "2025", "520", "888", "_", "official", "real"):
        add(h + suf)
    add("real" + h)
    add("the" + h)
    # 中文昵称的常见改写
    if not h.isascii():
        add(h.replace("的", ""))
        add(h + "子")
        add("小" + h)
        add("老" + h)
    return out


# ---------------------------------------------------------------- 证据台账

VERDICTS = {
    "confirmed": "已核实（有可直接引用的原文/官方数据，且能排除同名）",
    "likely": "高度可能（多条独立线索一致，但缺直接自述）",
    "guess": "推测（单一线索或弱证据）",
    "lead": "线索（只说明「某处出现过」，尚未指向具体人或事实）",
    "excluded": "已排除（找到反证）",
}

LEDGER_DEFAULT = "evidence.jsonl"


def ledger_add(path, claim, verdict, url, source="", quote="", date="", note=""):
    if verdict not in VERDICTS:
        raise SystemExit(f"verdict 必须是 {list(VERDICTS)} 之一")
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "claim": claim, "verdict": verdict, "url": url, "source": source,
        "quote": quote, "date": date, "note": note,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"已记入 {path}：[{verdict}] {claim}")


def ledger_render(path, out, subject=""):
    if not os.path.exists(path):
        raise SystemExit(f"没有台账文件 {path}，先用 ledger add 记几条。")
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    order = ["confirmed", "likely", "guess", "lead", "excluded"]
    groups = {k: [r for r in rows if r["verdict"] == k] for k in order}
    title = f"# 公开信息核查报告 · {subject}" if subject else "# 公开信息核查报告"
    lines = [title, ""]
    lines.append(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"- 证据条数：{len(rows)}（" + "，".join(f"{k} {len(groups[k])}" for k in order) + "）")
    lines += ["", "> 仅使用公开可访问页面。每条结论都必须能在下表找到出处；没有出处的判断不写进来。", ""]
    for k in order:
        lines.append(f"## {VERDICTS[k]}")
        lines.append("")
        if not groups[k]:
            lines += ["（暂无）", ""]
            continue
        lines += ["| # | 结论 | 来源 | 时间 | 原文摘录 |", "| --- | --- | --- | --- | --- |"]
        for i, r in enumerate(groups[k], 1):
            src = r.get("source") or urllib.parse.urlparse(r.get("url", "")).netloc
            link = f"[{src}]({r['url']})" if r.get("url") else src
            quote = (r.get("quote") or "").replace("|", "／")[:120]
            lines.append(f"| {i} | {r['claim']} | {link} | {r.get('date','')} | {quote} |")
        lines.append("")
    text = "\n".join(lines)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"报告已写出：{out}")
    else:
        print(text)


# ---------------------------------------------------------------- 自检

def diagnose_network():
    """先看网络出口，再看站点。

    很多「中文站搜不到」其实是流量被代理/加速器的 TUN 接管，出口 IP 被平台
    按异常网络风控。特征是域名被解析到 198.18.0.0/15（fake-ip）。
    """
    import socket
    probes = ["www.zhihu.com", "www.baidu.com", "www.xiaohongshu.com", "github.com"]
    res = {}
    for h in probes:
        try:
            res[h] = socket.gethostbyname(h)
        except OSError as e:
            res[h] = f"解析失败({e})"
    fake = [h for h, ip in res.items() if re.match(r"^198\.1[89]\.", str(ip))]
    return res, fake


def do_doctor(json_out=False):
    dns, fake = diagnose_network()
    checks = [
        ("bing", lambda: len(EngineBing().search("罗翔", limit=2))),
        ("360", lambda: len(Engine360().search("罗翔", limit=2))),
        ("weixin", lambda: len(SogouWeixin().search("罗翔", limit=2))),
        ("bili", lambda: len(Bili().user_search("罗翔", limit=1))),
        ("ddg", lambda: len(EngineDDG().search("罗翔", limit=2))),
    ]
    report = []
    for name, fn in checks:
        t0 = time.time()
        left = cooldown_left(name)
        if left:
            report.append({"source": name, "ok": False, "cooldown_s": left,
                           "error": f"冷却中，还剩 {left // 60} 分 {left % 60} 秒", "ms": 0})
            continue
        try:
            n = fn()
            report.append({"source": name, "ok": n > 0, "items": n,
                           "ms": int((time.time() - t0) * 1000)})
        except Blocked as e:
            report.append({"source": name, "ok": False, "error": f"被风控：{e}",
                           "ms": int((time.time() - t0) * 1000)})
        except Exception as e:  # noqa: BLE001
            report.append({"source": name, "ok": False, "error": f"{type(e).__name__}: {e}",
                           "ms": int((time.time() - t0) * 1000)})
    if json_out:
        print(json.dumps({"dns": dns, "fake_ip": fake, "sources": report}, ensure_ascii=False, indent=1))
    else:
        print("# 网络出口自检")
        for h, ip in dns.items():
            tag = "  <- fake-ip" if h in fake else ""
            print(f"  {h:22s} {ip}{tag}")
        if fake:
            print("\n! 有域名被解析到 198.18.x（代理的 fake-ip）。这说明国内流量也被代理接管了，")
            print("  平台会按「异常网络」直接风控——知乎 40362、小红书「当前网络存在风险」、")
            print("  微博跳登录、抖音出验证码，都是同一个原因。查国内站前先断开代理/加速器，")
            print("  或把国内域名设成直连。这条比任何反爬技巧都有效。\n")
        print("# 信源自检（当前网络环境下哪些能真正取到内容）")
        for r in report:
            flag = "OK  " if r["ok"] else "FAIL"
            extra = f"{r.get('items')} 条" if r["ok"] else r.get("error", "")
            print(f"  {flag} {r['source']:8s} {r.get('ms', 0):>5}ms  {extra}")
        ok = sum(1 for r in report if r["ok"])
        print(f"\n可用 {ok}/{len(report)}。"
              "被风控的源会进冷却（记在 ~/.cache/shiji/cooldown.json），别反复重试；"
              "确实要立刻用，就换网络出口（手机热点最有效）或改用浏览器打开。")
    return report


# ---------------------------------------------------------------- CLI

def main(argv=None):
    p = argparse.ArgumentParser(
        prog="shiji", description="拾迹：中文互联网公开信息检索（零依赖）")
    p.add_argument("--version", action="version", version=f"shiji {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("search", help="网页检索（多引擎合并去重）")
    sp.add_argument("query")
    sp.add_argument("--engine", default="auto", help="auto（默认，跳过冷却中的源）或 360,bing,ddg")
    sp.add_argument("--limit", type=int, default=8)
    sp.add_argument("--page", type=int, default=1)
    sp.add_argument("--host", default="", help="只要某个域名的结果，如 zhihu.com（site: 语法不可靠，这里做硬过滤）")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("wx", help="搜狗微信：公众号文章检索")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=8)
    sp.add_argument("--resolve", type=int, default=0, help="解开前 N 条的真实 mp.weixin 地址")
    sp.add_argument("--fetch", type=int, default=0, help="同一次运行里顺手抓前 N 条正文（推荐）")
    sp.add_argument("--max-chars", type=int, default=1500)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("fetch", help="抓取并抽正文")
    sp.add_argument("url")
    sp.add_argument("--max-chars", type=int, default=4000)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("bili-user", help="B站昵称搜索")
    sp.add_argument("keyword")
    sp.add_argument("--limit", type=int, default=8)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("bili-video", help="B站视频搜索")
    sp.add_argument("keyword")
    sp.add_argument("--limit", type=int, default=8)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("bili-space", help="B站 mid→简介/粉丝/作品数")
    sp.add_argument("mid")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("dossier", help="一条命令出昵称画像卡片（账号→自述线索→顺着查→内容）")
    sp.add_argument("nickname")
    sp.add_argument("--out", default="")
    sp.add_argument("--limit", type=int, default=6)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("probe", help="昵称可达性矩阵：哪些平台能按昵称查到、哪些是登录墙")
    sp.add_argument("nickname")
    sp.add_argument("--only", default="", help="只跑指定平台，逗号分隔")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("ids", help="标识符矩阵：把昵称摊成一组候选 ID")
    sp.add_argument("nickname")
    sp.add_argument("--handles", default="", help="已知的其他 ID，逗号分隔")
    sp.add_argument("--out", default="")

    sp = sub.add_parser("variants", help="昵称/ID 变体扩展")
    sp.add_argument("handle")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("links", help="生成各平台手动检索清单（抓不到时的正路）")
    sp.add_argument("keyword")
    sp.add_argument("--out", default="")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("ledger", help="证据台账：add / render")
    sp.add_argument("action", choices=["add", "render"])
    sp.add_argument("--file", default=LEDGER_DEFAULT)
    sp.add_argument("--claim")
    sp.add_argument("--verdict", default="lead")
    sp.add_argument("--url", default="")
    sp.add_argument("--source", default="")
    sp.add_argument("--quote", default="")
    sp.add_argument("--date", default="")
    sp.add_argument("--note", default="")
    sp.add_argument("--out", default="")
    sp.add_argument("--subject", default="")

    sp = sub.add_parser("doctor", help="信源自检")
    sp.add_argument("--json", action="store_true")

    args = p.parse_args(argv)

    try:
        if args.cmd == "search":
            do_search(args.query, args.engine, args.limit, args.json, args.page, args.host)
        elif args.cmd == "wx":
            w = SogouWeixin()
            items = w.search(args.query, limit=args.limit)
            for i in range(min(max(args.resolve, args.fetch), len(items))):
                try:
                    items[i]["real_url"] = w.resolve(items[i]["url"])
                except Exception as e:  # noqa: BLE001
                    items[i]["real_url"] = f"(未解开: {e})"
            if args.fetch:
                # 微信号的签名地址有时效，必须在同一个会话里解一条抓一条
                for it in items[:args.fetch]:
                    if not it.get("real_url", "").startswith("http"):
                        continue
                    try:
                        title, text = do_fetch(it["real_url"], args.max_chars)
                        it["full_title"] = title
                        it["text"] = text
                    except Exception as e:  # noqa: BLE001
                        it["text"] = f"(抓取失败: {e})"
            if args.json:
                print(json.dumps(items, ensure_ascii=False, indent=1))
            else:
                print(f"# 微信公众号文章：{args.query}（{len(items)} 条）")
                for i, it in enumerate(items, 1):
                    print(f"\n[{i}] {it['title']}")
                    print(f"    公众号：{it.get('account') or '—'}   日期：{it.get('date') or '—'}")
                    print(f"    {it.get('real_url') or it['url']}")
                    if it.get("snippet"):
                        print(f"    摘要：{it['snippet'][:200]}")
                    if it.get("text"):
                        print(f"\n    正文节选：{it['text'][:args.max_chars]}")
        elif args.cmd == "fetch":
            do_fetch(args.url, args.max_chars, args.json)
        elif args.cmd == "bili-user":
            items = Bili().user_search(args.keyword, args.limit)
            if args.json:
                print(json.dumps(items, ensure_ascii=False, indent=1))
            else:
                print(f"# B站用户：{args.keyword}（{len(items)} 条）")
                for it in items:
                    print(f"\n  {it['uname']}  mid={it['mid']}  粉丝={it['fans']}  等级={it['level']}")
                    print(f"    {it['url']}")
                    if it.get("sign"):
                        print(f"    签名：{it['sign'][:120]}")
        elif args.cmd == "bili-video":
            items = Bili().video_search(args.keyword, args.limit)
            if args.json:
                print(json.dumps(items, ensure_ascii=False, indent=1))
            else:
                print(f"# B站视频：{args.keyword}（{len(items)} 条）")
                for it in items:
                    print(f"\n  {it['title']}")
                    print(f"    UP：{it['author']}(mid={it['mid']})  播放：{it.get('play')}  {it['url']}")
        elif args.cmd == "bili-space":
            d = Bili().space(args.mid)
            if args.json:
                print(json.dumps(d, ensure_ascii=False, indent=1, default=str))
            else:
                print(f"# {d['name']} (mid={d['mid']})")
                print(f"  签名：{d.get('sign')}")
                print(f"  粉丝：{d.get('fans')}  关注：{d.get('attention')}  投稿：{d.get('archive_count')}")
                print(f"  主页：{d['url']}")
        elif args.cmd == "variants":
            vs = variants(args.handle)
            if args.json:
                print(json.dumps(vs, ensure_ascii=False, indent=1))
            else:
                print("\n".join(vs))
        elif args.cmd == "dossier":
            do_dossier(args.nickname, args.out, args.limit, args.json)
        elif args.cmd == "probe":
            do_probe(args.nickname, args.only, args.json)
        elif args.cmd == "ids":
            do_ids(args.nickname, args.handles, args.out)
        elif args.cmd == "links":
            do_links(args.keyword, args.out, args.json)
        elif args.cmd == "ledger":
            if args.action == "add":
                if not args.claim:
                    raise SystemExit("ledger add 需要 --claim")
                ledger_add(args.file, args.claim, args.verdict, args.url,
                           args.source, args.quote, args.date, args.note)
            else:
                ledger_render(args.file, args.out, args.subject)
        elif args.cmd == "doctor":
            do_doctor(args.json)
    except BudgetExceeded as e:
        print(f"预算用尽：{e}", file=sys.stderr)
        return 2
    except Blocked as e:
        print(f"该源不可用：{e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
