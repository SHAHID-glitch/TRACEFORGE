FROM python:3.12-alpine

COPY registry_proxy.py /registry_proxy.py

ENTRYPOINT ["python", "/registry_proxy.py"]
