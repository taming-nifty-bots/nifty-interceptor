# Start from python slim-buster docker image
FROM python:3.11.8-slim
# Update base packages
RUN apt-get update -y
RUN apt-get upgrade -y
# Change TimeZone
ENV TZ=Asia/Calcutta
ENV live_trading=false

ENV user_name=sugam

ENV instrument_name=NIFTY

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone
RUN echo $(date)
# Copy files to working directory
COPY requirements.txt executor.py /app/src/
WORKDIR /app
# Install python packages using requirements.txt
RUN pip install -r src/requirements.txt

# Run the script
CMD ["python", "src/executor.py"]