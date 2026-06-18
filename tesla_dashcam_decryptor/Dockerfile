ARG BUILD_FROM
FROM ${BUILD_FROM}

# Python + AES (py3-cryptography als fertiges Alpine-Paket -> kein Kompilieren auf ARM)
# cifs-utils fuer den SMB/NAS-Mount
RUN apk add --no-cache python3 py3-cryptography cifs-utils ffmpeg

WORKDIR /app
COPY app/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

# keys.json / Cache liegen im persistenten /data des Add-ons
ENV DATA_DIR=/data

CMD [ "/run.sh" ]
