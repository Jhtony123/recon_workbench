# 双进程并发写 jobs.jsonl 的 os.replace 竞态（WinError 5 拖垮整理作业）

| Field | Value |
|-------|-------|
| id | `2026-11-16-joblog-concurrent-replace-race` |
| date | 2026-11-16 |
| status | fixed |
| severity | medium |
| area | `rules_store`（joblog 文件态） |

## Symptom

10 月双进程并行整理（主批+vip）时 SPH005 报
`PermissionError [WinError 5] .jobs-xxx.tmp -> jobs.jsonl`，
账单本体已整理成功却因留痕写入失败整个用例判 EXC。

## Root cause

joblog 整文件原子重写（mkstemp+os.replace）只有进程内 threading.Lock，
跨进程无互斥；Windows 下另一进程/杀毒/索引器瞬时持有目标句柄时
os.replace 拒绝访问。留痕是辅助信息，不应让整理作业失败。

## Fix

_append_or_replace 的 os.replace 加 5 次短重试（0.15s 递增）；
仍失败则丢弃本条留痕并清理 tmp，**不抛错**。

## Files

- `core/joblog.py`

## Tests

SPH005 重跑成功对平；后续补跑批与 vip 重跑再无该错。

## Regression guard

留痕写失败绝不拖垮主作业；跨进程并发场景留痕允许丢条不允许抛栈。
