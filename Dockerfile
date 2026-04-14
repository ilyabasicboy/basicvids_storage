FROM python:3.10.12-slim AS builder

WORKDIR /install

RUN apt-get update && apt-get install -y build-essential

COPY requirements.txt .
RUN pip install --prefix=/install/deps -r requirements.txt


FROM python:3.10.12-slim

WORKDIR /basicvids_storage

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY --from=builder /install/deps /usr/local

RUN apt-get update && apt-get install -y \
    curl \
    less \
    nano \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN chmod +x entrypoint.sh

ENTRYPOINT ["/basicvids_storage/entrypoint.sh"]
