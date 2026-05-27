FROM apify/actor-python-playwright:3.12

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

ENV PYTHONPATH=/usr/src/app

CMD ["python", "-m", "src.main"]
