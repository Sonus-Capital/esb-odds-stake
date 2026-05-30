FROM apify/actor-python:3.12
RUN playwright install chromium
COPY . .
