# PostgreSQL 备份保留策略（GFS 分级 + 半年）

> 关联 PR：将原先 `find ... -mtime +30 -delete` 的一刀切 30 天清理，升级为
> **GFS（祖父-父-子）分级保留 + 半年(180 天)总上限**。
> 背景：原策略仅保留 30 天，无法满足半年回溯与按周/按月审计留痕需求。

## 策略定义

| 维度 | 保留规则 |
|---|---|
| 短期（每日） | 备份生成后 **≤ `daily`(默认 30) 天** 的每日全量备份全部保留 |
| 中期（分级） | **`daily` ~ `totalDays`(默认 180) 天** 之间，仅保留「**周日**」与「**每月 1 号**」锚点备份（GFS 的周/月祖父层） |
| 长期（硬上限） | **> `totalDays`(默认 180 天 = 半年)** 的备份直接删除，任何情况下不超过半年 |

> 半年 = 180 天。如需调整，改 values 的 `postgres.backup.retention` 即可，无需改模板。

## 配置位置（values.yaml）

两个 chart 的 `postgres.backup` 段均新增 `retention` 子配置：

```yaml
postgres:
  backup:
    enabled: true
    schedule: "0 2 * * *"
    hostPath: "/srv/hci/backups/postgres"
    retention:
      daily: 30        # 每日全量短期保留天数
      totalDays: 180   # 半年总保留上限
```

## 实现机制

CronJob（`deploy/helm/<chart>/templates/postgres/backup-cronjob.yaml`）每天 02:00 UTC
执行 `pg_dump | gzip` 落 `hostPath`，随后运行分级清理脚本：

1. `find /backup -name "*.sql.gz" -mtime +${KEEP_TOTAL} -delete` —— 半年硬上限；
2. 遍历剩余备份，对 age > `daily` 的非「周日」且非「每月 1 号」文件 `rm -f` —— 仅留周/月锚点。

脚本用文件 mtime（= 备份生成时刻）判定 age 与星期/日期，无需解析文件名。

## 已知残留风险（对抗性审查）

- **同盘无异地**：备份仍在 `hostPath` 单盘（与数据库同机），主机故障即备份与数据同毁。
  本策略只解决「保留时长/分级」，不解决「异地容灾」。长期建议引入 pgBackRest
  （增量 + PITR + 原生 S3 异地），见运维复盘。
- **两 chart 共享 hostPath**：`hci-platform` 与 `hci-platform-data` 默认都写同一
  `/srv/hci/backups/postgres`，分级清理互相可见。因两实例策略一致，最终状态收敛，
  但文件混合不利于按实例区分；如需隔离，给不同 `hostPath` 或加实例前缀。
- **存量回溯**：本策略上线后，已存在的 >180 天旧备份会在首次运行即被清除；
  30~180 天的非锚点文件也会被清掉，仅保留周日/1 号锚点。属预期行为。
