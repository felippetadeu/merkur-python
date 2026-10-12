# Imagem usada no Railway (e em qualquer host que rode Docker sem docker-compose).
FROM freqtradeorg/freqtrade:stable

COPY --chown=ftuser:ftuser user_data/ /freqtrade/user_data/
COPY --chown=ftuser:ftuser start.sh /freqtrade/start.sh

ENTRYPOINT ["/bin/bash", "/freqtrade/start.sh"]
