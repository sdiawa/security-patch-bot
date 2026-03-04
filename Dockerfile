FROM python:3.11-slim

RUN pip install --no-cache-dir \
  python-gitlab==4.10.0 \
  ruamel.yaml==0.18.6 \
  packaging==24.1 \
  fnmatch2==0.0.8

WORKDIR /app

COPY . /app
