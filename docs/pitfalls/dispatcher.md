# Dispatcher / 状态机 / 幂等资源管理避坑

## PIT-006：状态机转换未加分布式锁导致重复处理

并发场景下，状态转换前必须使用 `SELECT FOR UPDATE` 或 Redis 分布式锁，防止同一任务被多个 worker 同时处理。

## PIT-007：幂等键未覆盖所有入口

外部回调、重试队列、手动触发三个入口都需要幂等检查，只在其中一个入口加锁不够。

## PIT-008：Dispatcher 重启后未恢复 in-flight 任务

进程崩溃后处于 `running` 状态的任务不会自动回到队列，需要在启动时执行 `recover_stuck_tasks()`，把超时的 running 任务重置为 `pending`。
