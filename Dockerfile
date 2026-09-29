FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# The database is a file, so it belongs on a volume, not in the image.
VOLUME ["/data"]
ENV JOBMATCH_DB=/data/jobmatch.db

ENTRYPOINT ["jobmatch"]
CMD ["--help"]
