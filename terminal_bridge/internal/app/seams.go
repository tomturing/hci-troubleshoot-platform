// seams.go R0 结构性装配缝：由 main 注入，避免 app 反向依赖装配层。
package app

import "golang.org/x/net/websocket"

// WSHandlerFunc WS 消息分发入口（dispatch 暂留装配层，R0c 注册式路由后收编）。
var WSHandlerFunc func(b *Bridge, ws *websocket.Conn, customUI string)

// VersionFunc 返回注入的版本串（ldflags -X main.Version 要求版本变量留 package main）。
var VersionFunc = func() string { return "dev" }

// CommitIDFunc 返回注入的 git commit（ldflags -X main.CommitID）。
var CommitIDFunc = func() string { return "unknown" }

// BuildTimeFunc 返回注入的构建时间（ldflags -X main.BuildTime）。
var BuildTimeFunc = func() string { return "unknown" }
