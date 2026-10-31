FROM python:3.12-alpine
RUN apk add --no-cache ffmpeg tzdata
COPY movie_glow.py /app/movie_glow.py
USER 1000:1000
WORKDIR /config
ENV PYTHONUNBUFFERED=1 MOVIE_GLOW_CONFIG=/config/config.toml
ENTRYPOINT ["python3", "-I", "/app/movie_glow.py"]
