ARG SOURCE_SHA=unknown
ARG IMAGE_TAG=dev

# 使用当前受支持的 Go 补丁线，避免把已修复的标准库漏洞带入运行镜像。
FROM golang:1.26.6-alpine AS builder

ARG IMAGE_TAG
WORKDIR /src/hci_sim
COPY hci_sim/go.mod hci_sim/go.sum ./
RUN go mod download
COPY hci_sim/ ./
RUN CGO_ENABLED=0 GOOS=linux go build -buildvcs=false -trimpath \
    -ldflags "-s -w -X main.version=${IMAGE_TAG}" \
    -o /out/hci-sim ./cmd/hci-sim

# 运行时基础镜像跟随受支持的 Alpine 次版本，构建时由 Trivy 校验可修复漏洞。
FROM alpine:3.22

ARG SOURCE_SHA
ARG IMAGE_TAG
LABEL org.opencontainers.image.revision="${SOURCE_SHA}" \
      org.opencontainers.image.version="${IMAGE_TAG}"

RUN addgroup -g 1000 hci-sim && adduser -D -H -u 1000 -G hci-sim hci-sim
COPY --from=builder /out/hci-sim /usr/local/bin/hci-sim
RUN ln -s /usr/local/bin/hci-sim /usr/local/bin/acli
USER 1000:1000
EXPOSE 2222 8080
ENTRYPOINT ["/usr/local/bin/hci-sim"]
