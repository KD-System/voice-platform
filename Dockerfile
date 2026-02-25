FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# fs_cli wrapper (ESL-клиент для связи с FreeSWITCH из контейнера)
RUN cp scripts/fs_cli /usr/local/bin/fs_cli && chmod +x /usr/local/bin/fs_cli

EXPOSE 8000

ENTRYPOINT ["python", "web.py"]
CMD ["robots/pipeline_russian"]
