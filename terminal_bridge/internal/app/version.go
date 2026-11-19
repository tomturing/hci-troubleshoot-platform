// version.go 版本渲染：注入串经 VersionFunc 缝取自装配层（ldflags 约束）。
package app

import (
	"fmt"
	"runtime/debug"
	"strings"
)

func GetVersion() string {
	if !strings.HasSuffix(VersionFunc(), "-dev") {
		return VersionFunc()
	}
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return VersionFunc()
	}
	var revision string
	var modified bool
	for _, setting := range info.Settings {
		switch setting.Key {
		case "vcs.revision":
			revision = setting.Value
		case "vcs.modified":
			modified = setting.Value == "true"
		}
	}
	if revision == "" {
		return VersionFunc()
	}
	if len(revision) > 8 {
		revision = revision[:8]
	}
	dirty := ""
	if modified {
		dirty = "-dirty"
	}
	return fmt.Sprintf("%s-g%s%s", VersionFunc(), revision, dirty)
}
