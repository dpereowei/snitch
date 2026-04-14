#!/bin/bash

set -e

docker build -t snitch:latest --platform linux/amd64 .

docker tag snitch:latest realartisan/snitch:latest

docker push realartisan/snitch:latest

echo "Docker image 'realartisan/snitch:latest' has been built and pushed successfully."