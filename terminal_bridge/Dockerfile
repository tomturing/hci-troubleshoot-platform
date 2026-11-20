# terminal_bridge Linux 容器镜像；与 Windows EXE 使用同一份 Go 源码。
FROM golang:1.26.6-alpine AS build

WORKDIR /src

COPY terminal_bridge/go.mod terminal_bridge/go.sum ./
RUN go mod download

COPY terminal_bridge/ ./

ARG VERSION=v2.16.0-dev
ARG COMMIT_ID=unknown
ARG BUILD_TIME=unknown

# U1：集群镜像带 telemetry tag（完整 OTel 导出）；桌面 exe 不带 tag（noop，
# 编译期零 otel 链接，见 scripts/build_terminal_bridge.sh 与架构重构设计 §6.2）。
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
    -tags telemetry \
    -trimpath \
    -ldflags="-s -w -buildid= -X main.Version=${VERSION} -X main.CommitID=${COMMIT_ID} -X main.BuildTime=${BUILD_TIME}" \
    -o /out/terminal_bridge ./cmd/bridge

FROM alpine:3.22

RUN addgroup -g 1000 -S bridge \
    && adduser -u 1000 -S -D -H -G bridge bridge \
    && mkdir -p /var/lib/terminal-bridge \
    && chown -R bridge:bridge /var/lib/terminal-bridge

COPY --from=build /out/terminal_bridge /usr/local/bin/terminal_bridge

USER 1000:1000
EXPOSE 9999

ENTRYPOINT ["/usr/local/bin/terminal_bridge"]
CMD ["--mode=cluster"]
