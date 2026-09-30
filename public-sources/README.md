# 本地公开源适配器

日报中的 36氪与触乐使用 RSSHub 路由。公共实例会限速或返回 403，因此正式运行固定使用本地容器：

```bash
docker compose -f public-sources/docker-compose.yml up -d
curl -f http://127.0.0.1:1200/healthz
```

`config.env` 中保持：

```bash
RIBAO_RSSHUB_BASE="http://127.0.0.1:1200"
```

容器只监听本机回环地址；镜像使用已核验的不可变 `RepoDigest`，不会把可移动标签误称为固定版本。升级 digest 前必须重新验证 36氪和触乐路由。
