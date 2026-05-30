FROM apify/actor-python-playwright:3.12
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
