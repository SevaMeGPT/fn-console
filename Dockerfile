FROM python:3.11-slim
WORKDIR /app
COPY fn_console.py .
ENV WORKSPACE=/app/workspace ARCHIVE_DIR=/app/archive
RUN mkdir -p /app/workspace /app/archive && chmod -R 777 /app
EXPOSE 7860
CMD ["python", "fn_console.py"]
