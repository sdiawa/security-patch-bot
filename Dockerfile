FROM python:3.11-slim


WORKDIR /app
COPY requirements.txt /app/requirements.txt

# pip.conf est fourni en secret au build (pas dans les layers)
RUN --mount=type=secret,id=pip_conf,target=/etc/pip.conf \
    pip install --no-cache-dir -r /app/requirements.txt

COPY patch_bot.py /app/patch_bot.py
COPY config.yaml /app/config.yaml

CMD ["python", "patch_bot.py", "--config", "config.yaml"]
