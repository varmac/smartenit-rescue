FROM python:3.13-slim AS build

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.13-slim

RUN groupadd --gid 10001 rescue \
    && useradd --uid 10001 --gid rescue --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin rescue
COPY --from=build /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/smartenit_rescue-*.whl \
    && rm -rf /wheels

USER rescue
ENTRYPOINT ["smartenit-rescue", "run", "--config", "/etc/smartenit-rescue/config.toml"]
