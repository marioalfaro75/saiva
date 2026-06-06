FROM node:22-alpine AS build
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
