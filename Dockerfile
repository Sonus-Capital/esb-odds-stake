FROM apify/actor-python:3.12
COPY requirements.txt ./
RUN pip install -r requirements.txt
RUN python3 -m playwright install chromium --with-deps
COPY . .
