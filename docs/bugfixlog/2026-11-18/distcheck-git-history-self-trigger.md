# dist_check git 历史扫描自触发 blocked（扫描器模式字面量命中自身）

- 日期：2026-11-18
- 阶段：分发安全（core/distcheck.py）
- 严重度：高（安全闸误报 blocked，且首次出现在推送后的复检）

## 症状

首次 commit+push（679f945，含 core/distcheck.py 入库）后复检 dist_check：
verdict=blocked，git_history findings 两条 `private_key hit=core/distcheck.py`。
工作区扫描 0 命中——只有历史扫描命中，且命中的是扫描器自己。

## 根因

scan_git_history 的模式表写了**裸字面量** `"BEGIN RSA PRIVATE KEY"` /
`"BEGIN OPENSSH PRIVATE KEY"`（无 `-----` 前后缀）。distcheck.py 自身提交入库后，
`git grep` 在全历史里扫到 distcheck.py 的**模式定义行**——扫描器把自己判成私钥泄露。
与此前 verify 脚本「假秘密字面量自触发」是同一类坑，但当时只修了夹具侧，
漏了扫描器模式表自身也要防自触发。

## 修复（core/distcheck.py scan_git_history）

1. PEM 模式改为**运行时拼接** `"-----BEGIN " + "RSA PRIVATE KEY-----"`（与
   verify_distcheck 夹具同一纪律），历史里的源码行不再含完整可命中串；
2. 同时补全 `-----` 前后缀（真实 PEM 块格式），顺带收窄误报面并补
   EC / PKCS#8 两种头（原来只扫 RSA/OPENSSH 两种，反而是漏报面）。

## 验证

- scripts/verify_distcheck.py 24/24 PASS（含假 PEM 注入必须 blocked 的红测）；
- 修复提交后 MCP dist_check 复检 verdict=safe（工作区+git 全历史 0 命中）。

## 教训

**安全扫描器的检测模式本身也是被扫内容**：任何「秘密样字面量」（PEM 头、
假口令、已知秘密值）写进扫描器/验证脚本源码，入库后必然被自己扫到。
一律运行时拼接构造，且这条纪律要同时覆盖扫描器模式表，不只是测试夹具。
已同步 extension_creator playbook（plugin-dist-security §四）。