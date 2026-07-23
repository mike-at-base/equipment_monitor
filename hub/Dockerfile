# syntax=docker/dockerfile:1
#
# emhub — equipment-monitor v2 SCADA + UDP telemetry ingest.
#
# Reproducible multi-stage build from a clean checkout: compiles the React
# SPA, embeds it into a single static Go binary (go:embed all:dist), and
# ships it on a minimal distroless base. No toolchains in the final image.
#
# Build context is the hub/ directory (this file lives in hub/).

# ---- 1. build the React SPA -> web/dist ----
FROM node:24-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ---- 2. build the Go binary with the SPA embedded ----
FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
# always embed the freshly built UI, never a stale committed dist/
RUN rm -rf web/dist
COPY --from=web /web/dist ./web/dist
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /emhub ./cmd/emhub

# ---- 3. minimal runtime ----
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /emhub /emhub
# 8062/tcp = SCADA UI + REST/MCP API ; 15020/udp = PLC telemetry ingest
EXPOSE 8062/tcp
EXPOSE 15020/udp
ENTRYPOINT ["/emhub"]
CMD ["-config", "/config.yaml"]
