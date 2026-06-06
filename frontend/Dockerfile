# Build the (architecture-independent) static assets on the build host's native
# platform — emulating this stage under QEMU for arm64 intermittently crashes
# npm with "illegal instruction" (SIGILL). The final image below is still per-arch.
FROM --platform=$BUILDPLATFORM node:22-alpine AS build
ARG SAIVA_VERSION=dev
ENV VITE_SAIVA_VERSION=$SAIVA_VERSION
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
RUN npm run build

FROM caddy:2-alpine
COPY --from=build /app/dist /srv
COPY Caddyfile.web /etc/caddy/Caddyfile
EXPOSE 8080
