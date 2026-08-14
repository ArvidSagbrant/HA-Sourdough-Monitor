ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.22
FROM ${BUILD_FROM}

RUN apk add --no-cache \
      python3 \
      py3-pip \
      py3-numpy \
      py3-opencv \
      py3-requests \
      ffmpeg \
      && python3 -m venv --system-site-packages /opt/venv \
      && /opt/venv/bin/pip install --no-cache-dir paho-mqtt==2.1.0

RUN /opt/venv/bin/python -c "import cv2, numpy, requests; print('OpenCV', cv2.__version__, 'NumPy', numpy.__version__)"

COPY app.py /app/app.py
COPY ui.html /app/ui.html
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
