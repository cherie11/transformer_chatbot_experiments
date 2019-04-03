#!/bin/bash

IMAGE_NAME='transformer_chatbot'

sudo nvidia-docker run -it --rm -v $(pwd)/parameters:/workspace/parameters   \
                                -v $(pwd)/checkpoints:/workspace/checkpoints \
                                --entrypoint "python" $IMAGE_NAME "eval_f1.py"
