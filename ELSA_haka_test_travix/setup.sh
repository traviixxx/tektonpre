#!/bin/bash

# Exit immediately if a command exits with a non-zero status
set -e

# Function to install Docker
install_docker() {
    echo "Installing Docker..."
    apt-get update
    apt-get install -y apt-transport-https ca-certificates curl software-properties-common
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | apt-key add -
    add-apt-repository "deb [arch=amd64] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable"
    apt-get update
    apt-get install -y docker-ce
    systemctl start docker
    systemctl enable docker
    echo "Docker installation completed."
}

# Function to install Minikube
install_minikube() {
    echo "Installing Minikube..."
    curl -Lo minikube https://storage.googleapis.com/minikube/releases/latest/minikube-linux-amd64
    chmod +x minikube
    mv minikube /usr/local/bin/
    minikube start --driver=docker
    echo "Minikube installation completed."
}

# Function to install kubectl
install_kubectl() {
    echo "Installing kubectl..."
    curl -LO "https://storage.googleapis.com/kubernetes-release/release/$(curl -s https://storage.googleapis.com/kubernetes-release/release/stable.txt)/bin/linux/amd64/kubectl"
    chmod +x kubectl
    mv kubectl /usr/local/bin/
    echo "kubectl installation completed."
}

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root" 1>&2
    exit 1
fi

# Install Docker
install_docker

# Install Minikube
install_minikube

# Install kubectl
install_kubectl

# Run setup.sh
if [[ -f "./setup.sh" ]]; then
    echo "Running setup.sh..."
    chmod +x setup.sh
    ./setup.sh
else
    echo "setup.sh not found. Please ensure it is in the current directory."
fi

# Install Tekton full-stack

kubectl apply -f ./tekton-operator-v072.yaml

echo "Installation and setup completed."
