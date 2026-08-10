package database

import (
	"context"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// Open 建立 hci_sim 专用连接池，并在返回前执行一次 Ping。
// Target 已在 Parse 阶段拒绝 hci_troubleshoot；这里不接受任意 URL，避免
// Repository 层偷偷回退到平台主库。
func Open(ctx context.Context, target Target) (*pgxpool.Pool, error) {
	if !target.Configured {
		return nil, fmt.Errorf("hci-sim database is not configured")
	}
	config, err := pgxpool.ParseConfig(target.URL)
	if err != nil {
		return nil, fmt.Errorf("parse hci_sim database URL: %w", err)
	}
	config.MaxConns = 8
	config.MinConns = 1
	config.MaxConnLifetime = 30 * time.Minute
	config.MaxConnIdleTime = 5 * time.Minute
	pool, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		return nil, fmt.Errorf("open hci_sim database: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping hci_sim database: %w", err)
	}
	return pool, nil
}
