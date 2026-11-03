# DeckSupreme Airflow image: official Airflow 2.x + project runtime deps.
ARG AIRFLOW_VERSION=2.11.1
ARG PYTHON_VERSION=3.11
FROM apache/airflow:${AIRFLOW_VERSION}-python${PYTHON_VERSION}

ARG AIRFLOW_VERSION
ARG PYTHON_VERSION

COPY requirements.txt /tmp/requirements.txt

# Pin apache-airflow itself and resolve everything against the official
# constraints file so a dependency can never silently upgrade Airflow.
RUN pip install --no-cache-dir \
      "apache-airflow==${AIRFLOW_VERSION}" \
      -r /tmp/requirements.txt \
      --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"

# Project source is bind-mounted at /opt/airflow/src in dev (see docker-compose.yml).
ENV PYTHONPATH=/opt/airflow/src
