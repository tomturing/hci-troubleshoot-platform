// version.go 版本注入变量：ldflags -X main.Version/CommitID/BuildTime 要求留 package main。
package main

import "terminal_bridge/internal/app"

var (
	Version   = "v2.16.0-dev"
	CommitID  = "unknown" // 构建时通过 -X main.CommitID 注入 git commit
	BuildTime = "unknown" // 构建时通过 -X main.BuildTime 注入构建时间
)

// getVersion 薄包装：真实渲染在 app.GetVersion。
func getVersion() string { return app.GetVersion() }
