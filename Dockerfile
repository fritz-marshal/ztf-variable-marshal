FROM python:3.10.10

# Install: vim, git, cron
RUN apt-get update && apt-get -y install apt-file && apt-file update && apt-get -y install vim && \
    apt-get -y install cron && apt-get -y install git

# place to keep our app and the data:
RUN mkdir -p /app && mkdir -p /app/logs && mkdir -p /data && mkdir -p /_tmp

## Add crontab file in the cron directory
#ADD code/crontab /etc/cron.d/fetch-cron
## Give execution rights on the cron job
#RUN chmod 0644 /etc/cron.d/fetch-cron
## Apply cron job
#RUN crontab /etc/cron.d/fetch-cron
## Create the log file to be able to run tail
#RUN touch /var/log/cron.log

# install python libs
COPY ztf-variable-marshal/requirements.txt /app/
RUN pip install -r /app/requirements.txt

# copy over the secrets:
COPY secrets.json /app/

# copy over the code
ADD ztf-variable-marshal/ /app/

# change working directory to /app
WORKDIR /app

# generate keys
RUN python generate_secrets.py

# run tests
#RUN python -m pytest -s server.py

# run container
#CMD /bin/bash
#CMD /usr/local/bin/supervisord -n -c supervisord.conf
#CMD cron && crontab /etc/cron.d/fetch-cron && /bin/bash
CMD /usr/local/bin/gunicorn -w 8 --bind 0.0.0.0:4000 --worker-class aiohttp.GunicornWebWorker --worker-tmp-dir /dev/shm --max-requests 10000 server:app_factory
