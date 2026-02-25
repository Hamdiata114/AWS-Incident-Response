FROM python:3.12-slim
WORKDIR /app
COPY config/ /app/config/
COPY mcp/resolver/ /app/
RUN pip install --no-cache-dir -r requirements.txt
EXPOSE 8081
CMD ["python", "server.py"]
