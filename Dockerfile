FROM python:3.12-alpine
RUN apk add --no-cache inotify-tools
COPY app/ /app/
RUN printf '#!/bin/sh\nexec python3 /app/main.py "$@"\n' > /usr/local/bin/mover && chmod +x /usr/local/bin/mover
WORKDIR /app
ENV PYTHONUNBUFFERED=1
VOLUME ["/config"]
ENTRYPOINT ["mover"]
