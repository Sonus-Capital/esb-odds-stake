FROM apify/actor-python:3.12

COPY requirements.txt .
RUN pip install -r requirements.txt
RUN playwright install chromium

COPY . .

ENV PYTHONPATH=/usr/src/app

CMD ["python", "-m", "src.main"]
