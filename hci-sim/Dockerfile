# hci-sim P0 Spike 最小镜像
FROM golang:1.20-alpine AS build
WORKDIR /src
COPY go.mod ./
RUN go mod download || true
COPY . .
RUN CGO_ENABLED=0 go build -o /out/hci-sim .

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /out/hci-sim /usr/local/bin/hci-sim
EXPOSE 2222
ENTRYPOINT ["/usr/local/bin/hci-sim"]
CMD ["--listen", "0.0.0.0:2222", "--fixtures", "/fixtures", "--accept-any-password"]
