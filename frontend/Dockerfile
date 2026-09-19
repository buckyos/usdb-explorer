# Derived from Blockscout frontend v2.3.5 Dockerfile (GPL-3.0 license).
# Keep runtime scripts and locked dependencies aligned with frontend/upstream.lock.json.
ARG NODE_IMAGE
FROM ${NODE_IMAGE} AS source
RUN apk add --no-cache python3
COPY frontend /custom
RUN python3 /custom/prepare.py --output /source

# *****************************
# *** STAGE 1: Dependencies ***
# *****************************
FROM ${NODE_IMAGE} AS deps
# Check https://github.com/nodejs/docker-node/tree/b4117f9333da4138b03a546ec926ef50a31506c3#nodealpine to understand why libc6-compat might be needed.
RUN apk add --no-cache libc6-compat python3 make g++
RUN ln -sf /usr/bin/python3 /usr/bin/python

### APP
# Install dependencies
WORKDIR /app
COPY --from=source /source/package.json /source/yarn.lock /source/tsconfig.json ./
COPY --from=source /source/types ./types
COPY --from=source /source/lib ./lib
COPY --from=source /source/configs/app ./configs/app
COPY --from=source /source/toolkit/theme ./toolkit/theme
COPY --from=source /source/toolkit/utils ./toolkit/utils
COPY --from=source /source/toolkit/components/forms/validators/url.ts ./toolkit/components/forms/validators/url.ts
RUN apk add git
RUN yarn --frozen-lockfile --network-timeout 100000


### FEATURE REPORTER
# Install dependencies
WORKDIR /feature-reporter
COPY --from=source /source/deploy/tools/feature-reporter/package.json /source/deploy/tools/feature-reporter/yarn.lock ./
RUN yarn --frozen-lockfile --network-timeout 100000


### ENV VARIABLES CHECKER
# Install dependencies
WORKDIR /envs-validator
COPY --from=source /source/deploy/tools/envs-validator/package.json /source/deploy/tools/envs-validator/yarn.lock ./
RUN yarn --frozen-lockfile --network-timeout 100000

### FAVICON GENERATOR
# Install dependencies
WORKDIR /favicon-generator
COPY --from=source /source/deploy/tools/favicon-generator/package.json /source/deploy/tools/favicon-generator/yarn.lock ./
RUN yarn --frozen-lockfile --network-timeout 100000

### SITEMAP GENERATOR
# Install dependencies
WORKDIR /sitemap-generator
COPY --from=source /source/deploy/tools/sitemap-generator/package.json /source/deploy/tools/sitemap-generator/yarn.lock ./
RUN yarn --frozen-lockfile --network-timeout 100000

### MULTICHAIN CONFIG GENERATOR
# Install dependencies
WORKDIR /multichain-config-generator
COPY --from=source /source/deploy/tools/multichain-config-generator/package.json /source/deploy/tools/multichain-config-generator/yarn.lock ./
RUN yarn --frozen-lockfile --network-timeout 100000


# *****************************
# ****** STAGE 2: Build *******
# *****************************
FROM ${NODE_IMAGE} AS builder
RUN apk add --no-cache --upgrade libc6-compat bash jq

# pass build args to env variables
ARG GIT_COMMIT_SHA
ENV NEXT_PUBLIC_GIT_COMMIT_SHA=$GIT_COMMIT_SHA
ARG GIT_TAG
ENV NEXT_PUBLIC_GIT_TAG=$GIT_TAG
ARG NEXT_OPEN_TELEMETRY_ENABLED
ENV NEXT_OPEN_TELEMETRY_ENABLED=$NEXT_OPEN_TELEMETRY_ENABLED

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

### APP
# Copy dependencies and source code
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY --from=source /source/. .

# Build SVG sprite and generate .env.registry with ENVs list and save build args into .env file
RUN set -a && \
    source ./deploy/scripts/build_sprite.sh && \
    ./deploy/scripts/collect_envs.sh ./docs/ENVS.md && \
    set +a

# Next.js collects completely anonymous telemetry data about general usage.
# Learn more here: https://nextjs.org/telemetry
# Uncomment the following line in case you want to disable telemetry during the build.
# ENV NEXT_TELEMETRY_DISABLED 1

# Build app for production
ENV NODE_OPTIONS="--max-old-space-size=4096"
RUN yarn build


### FEATURE REPORTER
# Copy dependencies and source code, then build
COPY --from=deps /feature-reporter/node_modules ./deploy/tools/feature-reporter/node_modules
RUN cd ./deploy/tools/feature-reporter && yarn compile_config
RUN cd ./deploy/tools/feature-reporter && yarn build


### ENV VARIABLES CHECKER
# Copy dependencies and source code, then build 
COPY --from=deps /envs-validator/node_modules ./deploy/tools/envs-validator/node_modules
RUN cd ./deploy/tools/envs-validator && yarn build


### FAVICON GENERATOR
# Copy dependencies and source code
COPY --from=deps /favicon-generator/node_modules ./deploy/tools/favicon-generator/node_modules


### SITEMAP GENERATOR
# Copy dependencies and source code
COPY --from=deps /sitemap-generator/node_modules ./deploy/tools/sitemap-generator/node_modules

### MULTICHAIN CONFIG GENERATOR
# Copy dependencies and source code, then build 
COPY --from=deps /multichain-config-generator/node_modules ./deploy/tools/multichain-config-generator/node_modules
RUN cd ./deploy/tools/multichain-config-generator && yarn build


# *****************************
# ******* STAGE 3: Run ********
# *****************************
# Production image, copy all the files and run next
FROM ${NODE_IMAGE} AS runner
RUN apk add --no-cache --upgrade bash curl jq unzip

### APP
WORKDIR /app

# Uncomment the following line in case you want to disable telemetry during runtime.
# ENV NEXT_TELEMETRY_DISABLED 1

RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

# Set the correct permission for prerender cache
RUN mkdir .next
RUN chown nextjs:nodejs .next

COPY --from=builder /app/next.config.js ./
COPY --from=builder /app/public ./public
COPY --from=builder /app/package.json ./package.json
COPY --from=source /source/LICENSE ./LICENSE.blockscout

# Copy tools
COPY --from=builder /app/deploy/tools/envs-validator/index.js ./envs-validator.js
COPY --from=builder /app/deploy/tools/feature-reporter/index.js ./feature-reporter.js
COPY --from=builder /app/deploy/tools/multichain-config-generator/dist ./deploy/tools/multichain-config-generator/dist

# Copy scripts
## Entripoint
COPY --from=source --chmod=755 /source/deploy/scripts/entrypoint.sh .
## ENV validator and client script maker
COPY --from=source --chmod=755 /source/deploy/scripts/validate_envs.sh .
COPY --from=source --chmod=755 /source/deploy/scripts/make_envs_script.sh .
## Assets downloader
COPY --from=source --chmod=755 /source/deploy/scripts/download_assets.sh .
## OG image generator
COPY --from=source /source/deploy/scripts/og_image_generator.js .
## Favicon generator
COPY --from=source --chmod=755 /source/deploy/scripts/favicon_generator.sh .
COPY --from=builder /app/deploy/tools/favicon-generator ./deploy/tools/favicon-generator
RUN ["chmod", "-R", "777", "./deploy/tools/favicon-generator"]
RUN ["chmod", "-R", "777", "./public"]
## Sitemap generator
COPY --from=source --chmod=755 /source/deploy/scripts/sitemap_generator.sh .
COPY --from=builder /app/deploy/tools/sitemap-generator ./deploy/tools/sitemap-generator

# Copy ENVs files
COPY --from=builder /app/.env.registry .
COPY --from=builder /app/.env .

# Copy ENVs presets
ARG ENVS_PRESET
ENV ENVS_PRESET=$ENVS_PRESET
COPY --from=source /source/configs/envs ./configs/envs

# Automatically leverage output traces to reduce image size
# https://nextjs.org/docs/advanced-features/output-file-tracing
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

ENTRYPOINT ["./entrypoint.sh"]

USER nextjs

EXPOSE 3000

ENV PORT=3000

CMD ["node", "server.js"]
