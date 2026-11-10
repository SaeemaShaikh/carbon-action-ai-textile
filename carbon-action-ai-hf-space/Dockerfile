# Carbon-Action AI - Hugging Face Spaces build
FROM python:3.12.7-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=7860 \
    NO_BROWSER=1 \
    CARBON_DATA_DIR=/home/user/data \
    DEMO_MODE=1 \
    CLOCK=realtime \
    TICK_SECONDS=2 \
    AUTO_FAULTS=1 \
    FAULT_EVERY_HOURS=8 \
    SIM_CREW_MISS=0.05 \
    REPLAY_SECONDS=2

# Spaces run the container as user 1000
RUN useradd --uid 1000 --create-home user
WORKDIR /home/user/app/backend

COPY --chown=user backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user backend/ /home/user/app/backend/
COPY --chown=user frontend/ /home/user/app/frontend/
RUN mkdir -p /home/user/data && chown -R user:user /home/user

USER user
EXPOSE 7860
CMD ["python", "run.py"]
