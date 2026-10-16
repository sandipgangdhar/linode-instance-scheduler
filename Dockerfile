# syntax=docker/dockerfile:1.7
#
# Container image for the Linode Instance Scheduler (the LKE / Kubernetes deployment option).
# It runs the same program as the VM deployment; only the packaging and where its persistent
# state lives differ.
#
# The image is built from a release archive: the compiled program, its libraries and the built
# web dashboard. Download linode-instance-scheduler-linux-amd64.tar.gz (and its .sha256) from the
# GitHub release you want, put it next to this file, check it, then build:
#
#   sha256sum -c linode-instance-scheduler-linux-amd64.tar.gz.sha256
#   docker build --platform linux/amd64 -t <registry>/linode-instance-scheduler:<version> .
#
# An official image is published on every release to
# ghcr.io/sandipgangdhar/linode-instance-scheduler (:latest and :vX.Y.Z), so building it yourself
# is optional -- see the LKE deployment guide.
#
# Always pass --platform: a plain `docker build` on Apple Silicon makes an arm64 image that an
# amd64 LKE node can't run.
#
# No credentials are baked in. LINODE_API_TOKEN and every other secret are supplied at runtime as
# environment variables (see k8s/secret.example.yaml), never as a build argument.
#
# The same image runs every role (the scheduler, the REST API and dashboard, a one-off CLI
# command): which one is decided by the command Kubernetes passes it (k8s/deployment.yaml).
# State (the database, logs, trusted host keys) lives in /app/state, the volume mount point.

FROM debian:12-slim AS unpack
ARG ARCHIVE=linode-instance-scheduler-linux-amd64.tar.gz
COPY ${ARCHIVE} /tmp/release.tar.gz
RUN mkdir -p /app \
    && tar -xzf /tmp/release.tar.gz -C /app --strip-components=1 \
    && test -x /app/linode-instance-scheduler \
    && mkdir -p /app/state

FROM debian:12-slim

# ssh for reaching managed instances (and the Lish console); CA certificates for the Linode API
# and Object Storage.
RUN apt-get update && apt-get install -y --no-install-recommends \
      openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# A fixed, non-root user (uid/gid 1000): the Deployment's securityContext runs as this user, and
# fsGroup makes the mounted state volume writable by it.
RUN groupadd --gid 1000 scheduler \
    && useradd --create-home --uid 1000 --gid 1000 --shell /usr/sbin/nologin scheduler

COPY --from=unpack /app /app
RUN chown scheduler:scheduler /app/state

WORKDIR /app
USER scheduler

ENTRYPOINT ["/app/linode-instance-scheduler"]
CMD ["--help"]
