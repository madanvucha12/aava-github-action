FROM alpine:3.21

RUN apk update && apk upgrade

WORKDIR /plugin

COPY . /plugin/

RUN echo "Plugin Files"

RUN ls -R /plugin

CMD ["sh"]