# terminal_bridge Linux 容器镜像；与 Windows EXE 使用同一份 Go 源码。
FROM golang:1.26-alpine AS build

WORKDIR /src

COPY terminal_bridge/go.mod terminal_bridge/go.sum ./
RUN go mod download

COPY terminal_bridge/ ./

ARG VERSION=v2.16.0-dev
ARG COMMIT_ID=unknown
ARG BUILD_TIME=unknown

RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
    -trimpath \
    -ldflags="-s -w -buildid= -X main.Version=${VERSION} -X main.CommitID=${COMMIT_ID} -X main.BuildTime=${BUILD_TIME}" \
    -o /out/terminal_bridge .

FROM alpine:3.21

RUN addgroup -g 1000 -S bridge \
    && adduser -u 1000 -S -D -H -G bridge bridge \
    && mkdir -p /var/lib/terminal-bridge \
    && chown -R bridge:bridge /var/lib/terminal-bridge

COPY --from=build /out/terminal_bridge /usr/local/bin/terminal_bridge

USER 1000:1000
EXPOSE 9999

ENTRYPOINT ["/usr/local/bin/terminal_bridge"]
CMD ["--mode=cluster"]
