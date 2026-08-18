# 使用当前受支持的 Go 补丁线，避免把已修复的标准库漏洞带入运行镜像。
FROM golang:1.26.6-alpine AS builder

WORKDIR /src/hci_sim
COPY hci_sim/go.mod hci_sim/go.sum ./
RUN go mod download
COPY hci_sim/ ./
ARG VERSION=dev
RUN CGO_ENABLED=0 GOOS=linux go build -buildvcs=false -trimpath \
    -ldflags "-s -w -X main.version=${VERSION}" \
    -o /out/hci-sim ./cmd/hci-sim

# 运行时基础镜像跟随受支持的 Alpine 次版本，构建时由 Trivy 校验可修复漏洞。
FROM alpine:3.22

RUN addgroup -g 1000 hci-sim && adduser -D -H -u 1000 -G hci-sim hci-sim
COPY --from=builder /out/hci-sim /usr/local/bin/hci-sim
RUN ln -s /usr/local/bin/hci-sim /usr/local/bin/acli
USER 1000:1000
EXPOSE 2222 8080
ENTRYPOINT ["/usr/local/bin/hci-sim"]
