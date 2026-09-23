#!/bin/sh
# Runs INSIDE the guest. Turns a source-snapshot disk into a warm base:
# dockerd's data root, the pnpm store and node_modules all land on /work,
# so the whole warm set is one block device the next run can mount read-only.
set -ex
mkdir -p /work/docker-root /work/.pnpm-store
jq '. + {"data-root":"/work/docker-root"}' /etc/docker/daemon.json > /tmp/d.json
mv /tmp/d.json /etc/docker/daemon.json
systemctl restart docker
docker info | grep "Docker Root Dir"

cd /work
pnpm config set store-dir /work/.pnpm-store --location project
time pnpm install --frozen-lockfile

for i in postgres:16 edoburu/pgbouncer:latest ghcr.io/neondatabase/wsproxy:latest; do
  time docker pull "$i"
done
docker images
du -sh /work/node_modules /work/.pnpm-store /work/docker-root
sync
