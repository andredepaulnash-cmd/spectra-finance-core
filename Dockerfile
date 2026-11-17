FROM python:3.12.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY spectra_alpha2 ./spectra_alpha2
RUN python -m pip install --no-cache-dir .

RUN mkdir -p /data
VOLUME ["/data"]

ENTRYPOINT ["python","-m","spectra_alpha2"]
CMD ["run"]
