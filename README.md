# 顶想云知识库爬虫

从 [顶想云 (k.topthink.com)](https://k.topthink.com) 抓取整个知识库，按目录树顺序把全部文章合并输出为一个 Markdown 文件，并把文章中的图片/附件下载到本地。

## 功能特点

- 🔐 令牌登录：通过文档令牌换取授权会话
- 📚 全量抓取：自动解析目录树，按层级生成 `#`/`##`/`###` 标题
- 🖼️ 附件本地化：下载正文引用的图片/附件，并把引用路径改写到本地
- 🔗 站内跳转：指向站内其他文章的 `.md` 链接自动改写为文内锚点，单文件内可跳转
- ⏸️ 断点续传：每篇文章抓取成功即落盘缓存，中断后重跑自动跳过已抓取内容
- 🚦 限速与重试：请求间随机延时，网络错误/限流(429)/5xx 自动指数退避重试

## 工作原理

1. **登录**：`POST /@<空间>/token`，body 为 `token=<文档令牌>`，服务端返回空 200 并下发授权的 PHPSESSID cookie。
2. **解析**：每个文档页 HTML 内含 `<script type="application/payload+json">`，payload 包含：
   - `summary`：目录树
   - `lfs`：附件相对路径 → oid 映射
   - `config`：知识库标题
   - `file`：当前文章正文（本身即 Markdown）
3. **下载附件**：通过 `https://lfs.k.topthink.com/lfs/{oid}` 下载（无需登录态）。

## 安装

```bash
pip install -r requirements.txt
```

依赖：Python 3，`requests >= 2.31`。

## 用法

```bash
python crawler.py                      # 使用默认令牌，全量抓取
python crawler.py --token <令牌>       # 指定文档令牌
python crawler.py --limit 3            # 只抓前 3 篇文章（调试用）
python crawler.py --skip-assets        # 不下载图片/附件
python crawler.py --out-dir out        # 自定义输出目录
```

| 参数 | 说明 |
| --- | --- |
| `--token` | 文档令牌（默认使用脚本内置令牌） |
| `--out-dir` | 输出目录（默认 `output`） |
| `--skip-assets` | 不下载图片/附件 |
| `--limit N` | 只抓前 N 篇文章，调试用 |

## 输出

```
output/
├── 知识库名.md        # 全部文章按目录树顺序合并的 Markdown
├── _cache.json        # 断点缓存（文章正文 + 附件映射）
└── assets/            # 下载的图片/附件
```

## 注意事项

- 令牌可能过期，若抓取时报「未找到 payload」错误，请重新获取文档令牌后通过 `--token` 传入。
- 抓取全程自动限速（每请求间隔 0.4~1.0 秒），请勿移除，避免触发站点限流。
