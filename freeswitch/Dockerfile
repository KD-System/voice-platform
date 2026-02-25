FROM ghcr.io/patrickbaus/freeswitch-docker

# Python для audio_bridge.py (FIFO → WebSocket мост, без внешних зависимостей)
RUN apk add --no-cache python3

# Lua-скрипты (ws_bridge.lua + audio_bridge.py)
COPY scripts/ /usr/share/freeswitch/scripts/

# Dialplan: маршрутизация номеров 5200/5210/5203 к роботам
COPY conf/dialplan/default/01_robots.xml /etc/freeswitch/dialplan/default/01_robots.xml
