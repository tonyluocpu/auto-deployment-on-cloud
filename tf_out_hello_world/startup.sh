#!/bin/bash

# Update package list and install Python and pip
sudo apt-get update
sudo apt-get install -y python3 python3-pip git

# Clone the repository
git clone https://github.com/Arvo-AI/hello_world.git
cd hello_world

# Install the application's dependencies
pip3 install -r app/requirements.txt

# Set environment variables if needed
# export SOME_ENV_VAR=value

# Run the application
python3 app/app.py
