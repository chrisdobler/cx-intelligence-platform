# PostgreSQL 16 with pgvector, plus our first-boot init scripts.
#
# The init scripts are baked into the image (COPY) rather than bind-mounted, so
# database initialization does not depend on host file-sharing behaviour
# (single-file bind mounts are unreliable on Docker Desktop / macOS). Anything
# in /docker-entrypoint-initdb.d runs once, on first container start.
FROM pgvector/pgvector:pg16

COPY initdb/ /docker-entrypoint-initdb.d/
