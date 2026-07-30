FROM golang:1.24-alpine AS builder

WORKDIR /src/hci_sim
COPY hci_sim/go.mod hci_sim/go.sum ./
RUN go mod download
COPY hci_sim/ ./
ARG VERSION=dev
RUN CGO_ENABLED=0 GOOS=linux go build -buildvcs=false -trimpath \
    -ldflags "-s -w -X main.version=${VERSION}" \
    -o /out/hci-sim ./cmd/hci-sim

FROM alpine:3.21

RUN addgroup -g 1000 hci-sim && adduser -D -H -u 1000 -G hci-sim hci-sim
COPY --from=builder /out/hci-sim /usr/local/bin/hci-sim
USER 1000:1000
EXPOSE 2222 8080
ENTRYPOINT ["/usr/local/bin/hci-sim"]
