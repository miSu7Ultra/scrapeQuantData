#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
顶想云知识库爬虫

从 https://k7pl9l9npy.k.topthink.com/@xzrnk8gbr7 登录并抓取整个知识库，
按目录树顺序把全部文章合并输出为一个 Markdown 文件，图片/附件下载到本地。

工作原理（基于对站点行为的观察）：
  1. 登录：POST /@xzrnk8gbr7/token，body 为 token=<文档令牌>，
     服务端返回空 200 并下发授权的 PHPSESSID cookie（用 Session 保持即可）。
  2. 每个文档页 HTML 内含 <script type="application/payload+json">，
     payload 包含：summary（目录树）、lfs（附件: 相对路径 -> oid 映射）、
     config（文档标题）、file（当前文章正文，本身即 markdown）。
  3. 附件通过 https://lfs.k.topthink.com/lfs/{oid} 下载（无需登录态）。

用法:
    python crawler.py                      # 使用默认令牌，全量抓取
    python crawler.py --token <令牌>       # 指定令牌
    python crawler.py --limit 3            # 只抓前 3 篇文章（调试用）
    python crawler.py --skip-assets        # 不下载图片/附件
    python crawler.py --out-dir out        # 自定义输出目录
"""

import argparse
import json
import os
import random
import re
import sys
import time
from urllib.parse import quote, unquote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://k7pl9l9npy.k.topthink.com"
SPACE = "/@xzrnk8gbr7"          # 知识库空间路径
LFS_URL = "https://lfs.k.topthink.com/lfs/{oid}"
DEFAULT_TOKEN = "****"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

PAYLOAD_RE = re.compile(r'<script type="application/payload\+json">(.*?)</script>', re.S)
# 匹配 markdown 内联引用: ![](path) 或 [text](path)，分组1为路径
LINK_RE = re.compile(r'(!?\[[^\]]*\]\()([^)\s]+)(\))')


class TopThinkCrawler:
    def __init__(self, token, out_dir="output", skip_assets=False, limit=None):
        self.token = token
        self.out_dir = out_dir
        self.skip_assets = skip_assets
        self.limit = limit

        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA
        # 网络层重试: 最多 5 次，指数退避；对连接错误/限流(429)/5xx 都重试
        retry = Retry(
            total=5,
            connect=5,
            read=5,
            status=5,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "POST"]),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

        self.lfs_files = {}          # 附件相对路径 -> oid
        self.title = "知识库"         # 文档标题（来自 payload.config）
        self.articles = []           # 展平后的文章列表 [(depth, title, path, ...)]

        self.assets_dir = os.path.join(out_dir, "assets")
        self.cache_file = os.path.join(out_dir, "_cache.json")
        self.asset_map = {}          # 原始引用路径 -> 本地相对路径
        self.cache = {}              # 文章 path -> (标题, 正文)
        self.failed_articles = []    # 抓取失败的文章
        self.failed_assets = []      # 下载失败的附件

    def _throttle(self):
        """请求间限速，避免触发站点限流。"""
        time.sleep(random.uniform(0.4, 1.0))

    # ---------------- 网络层 ----------------

    def _get(self, url, **kw):
        kw.setdefault("timeout", 60)
        self._throttle()
        resp = self.session.get(url, **kw)
        resp.raise_for_status()
        return resp

    def get_payload(self, page_path):
        """GET 一个文档页并解析出 payload JSON。"""
        url = f"{BASE_URL}{SPACE}/{quote(page_path, safe='/')}"
        resp = self._get(url)
        m = PAYLOAD_RE.search(resp.text)
        if not m:
            raise RuntimeError(
                f"页面 {page_path} 中未找到 payload，登录态可能已失效或令牌错误")
        return json.loads(m.group(1))

    def login(self):
        """提交文档令牌，换取授权会话。"""
        url = f"{BASE_URL}{SPACE}/token"
        self._throttle()
        resp = self.session.post(url, data={"token": self.token}, timeout=30)
        resp.raise_for_status()
        # 用首个文档页验证登录态是否生效
        home = self.get_payload("daoyu.html")
        self.title = home["config"].get("title", "知识库")
        self.lfs_files = home.get("lfs", {}).get("files", {})
        print(f"[ok] 登录成功，知识库: {self.title}，附件 {len(self.lfs_files)} 个")
        return home

    # ---------------- 断点缓存 ----------------

    def load_cache(self):
        if os.path.exists(self.cache_file):
            with open(self.cache_file, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "articles" in data:
                # 新格式: 同时缓存正文与附件映射，续传时不重复下载附件
                self.cache = data["articles"]
                self.asset_map.update(data.get("assets", {}))
            else:
                self.cache = data  # 旧格式: 直接是 文章 path -> 正文 的映射

    def save_cache(self):
        os.makedirs(self.out_dir, exist_ok=True)
        with open(self.cache_file, "w", encoding="utf-8") as f:
            json.dump({"articles": self.cache, "assets": self.asset_map},
                      f, ensure_ascii=False)

    # ---------------- 目录树 ----------------

    def build_tree(self, nodes, depth=1):
        """
        展平 summary 目录树。
        节点结构: 有 path 的是文章节点（正文 + children），无 path 的是纯分组。
        """
        for node in nodes:
            if "path" in node:
                self.articles.append({
                    "depth": depth,
                    "title": node.get("title", ""),
                    "path": node["path"],
                })
                self.build_tree(node.get("children", []), depth + 1)
            else:
                self.build_tree(node.get("articles", []), depth)

    # ---------------- 抓取正文 ----------------

    def fetch_article(self, article):
        payload = self.get_payload(article["path"])
        content = payload.get("file", {}).get("content", "")
        # 各页面可能带各自的附件映射，合并以防首页 lfs 不完整
        self.lfs_files.update(payload.get("lfs", {}).get("files", {}))
        return content

    # ---------------- 附件下载 ----------------

    def download_assets(self, content):
        """下载正文中引用的附件，返回替换引用后的内容。"""
        if self.skip_assets:
            return content

        def replace(match):
            prefix, ref, suffix = match.groups()
            ref = ref.strip()
            # 外链/锚点/邮件链接不处理
            if ref.startswith(("http://", "https://", "#", "mailto:")):
                return match.group(0)
            local = self._fetch_asset(ref)
            if local is None:
                return match.group(0)
            return f"{prefix}{local}{suffix}"

        return LINK_RE.sub(replace, content)

    def _fetch_asset(self, ref):
        """按引用路径下载一个附件，返回本地相对路径；已下载过的直接返回。"""
        if ref in self.asset_map:
            return self.asset_map[ref]
        oid = self.lfs_files.get(ref)
        if not oid and ref.startswith("/"):
            # 站内绝对路径引用（如 /.topwrite/assets/xx.png）: 去掉前导 / 再查映射
            oid = self.lfs_files.get(ref.lstrip("/"))
        if not oid:
            return None  # 不在附件映射里（可能是站外路径/已失效链接）

        name = os.path.basename(ref.rstrip("/"))
        if not name:
            name = "asset"
        local_path = os.path.join(self.assets_dir, name)
        # 同名文件已存在（此前抓取已下载）: 直接复用，不重复下载也不生成 _1 副本
        if os.path.exists(local_path):
            rel = os.path.relpath(local_path, self.out_dir)
            self.asset_map[ref] = rel
            return rel

        url = LFS_URL.format(oid=oid)
        try:
            resp = self._get(url)
            os.makedirs(self.assets_dir, exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(resp.content)
        except Exception as e:
            print(f"    [warn] 附件下载失败 {ref}: {e}")
            self.failed_assets.append(ref)
            return None

        rel = os.path.relpath(local_path, self.out_dir)
        self.asset_map[ref] = rel
        print(f"    [asset] {ref} -> {rel} ({len(resp.content)}B)")
        return rel

    # ---------------- 站内页面链接 ----------------

    PAGE_LINK_RE = re.compile(
        r'(!?\[[^\]]*\]\()\s*<?([^)\s]*?(?:\([^)]*\)[^)\s]*?)*\.md)>?(\s*\))')

    def convert_page_links(self, content):
        """把指向站内其他文章的 .md 链接改写成文内锚点（合并单文件后仍可跳转）。"""
        titles = {a["title"] for a in self.articles}

        def replace(match):
            prefix, ref, suffix = match.groups()
            name = unquote(ref.strip())
            if name.endswith(".md"):
                name = name[:-3]
            if name in titles:
                return f"{prefix}#{name}{suffix}"
            return match.group(0)

        return self.PAGE_LINK_RE.sub(replace, content)

    # ---------------- 合并输出 ----------------

    def build_markdown(self):
        """把全部文章按目录树顺序合并成一个 Markdown。"""
        os.makedirs(self.out_dir, exist_ok=True)
        lines = [f"# {self.title}", ""]

        for i, art in enumerate(self.articles):
            if self.limit and i >= self.limit:
                break
            path = art["path"]
            if path in self.cache:  # 断点续传: 命中缓存直接复用
                content = self.cache[path]
            else:
                try:
                    content = self.fetch_article(art)
                    self.cache[path] = content
                    self.save_cache()  # 每篇成功即落盘，中断后可续传
                except Exception as e:
                    print(f"[error] 抓取失败: {path} ({art['title']}): {e}")
                    self.failed_articles.append(path)
                    continue
            content = self.download_assets(content)
            content = self.convert_page_links(content).strip("\n")

            # 标题级别: 深度1 -> #，2 -> ## ...
            level = min(art["depth"], 6)
            heading = f"{'#' * level} {art['title']}"
            print(f"[ok] ({i + 1}/{len(self.articles)}) {heading} [{path}]")
            lines.append("")
            lines.append(heading)
            lines.append("")
            lines.append(content)
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    # ---------------- 入口 ----------------

    def run(self):
        self.load_cache()
        home = self.login()
        self.build_tree(home["summary"])
        total = len(self.articles)
        print(f"[ok] 目录树共 {total} 篇文章")

        markdown = self.build_markdown()
        self.save_cache()  # 落盘附件映射，避免下次重跑重复下载/生成 _1 副本
        out_file = os.path.join(self.out_dir, f"{self.title}.md")
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(markdown)

        print("-" * 50)
        print(f"[done] 已输出: {out_file} ({len(markdown)} 字符)")
        if self.failed_articles:
            print(f"[warn] {len(self.failed_articles)} 篇文章失败: {self.failed_articles}")
        if self.failed_assets:
            print(f"[warn] {len(self.failed_assets)} 个附件失败: {self.failed_assets}")


def main():
    parser = argparse.ArgumentParser(description="顶想云知识库爬虫")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="文档令牌")
    parser.add_argument("--out-dir", default="output", help="输出目录 (默认 output)")
    parser.add_argument("--skip-assets", action="store_true", help="不下载图片/附件")
    parser.add_argument("--limit", type=int, default=None, help="只抓前 N 篇文章 (调试)")
    args = parser.parse_args()

    crawler = TopThinkCrawler(
        token=args.token,
        out_dir=args.out_dir,
        skip_assets=args.skip_assets,
        limit=args.limit,
    )
    try:
        crawler.run()
    except requests.HTTPError as e:
        print(f"[error] 网络请求失败: {e}")
        sys.exit(1)
    except RuntimeError as e:
        print(f"[error] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()