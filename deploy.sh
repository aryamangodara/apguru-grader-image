#!/bin/bash
# EC2 deployment script for Amazon Linux 2023 / Ubuntu
# Run as: sudo bash deploy.sh
set -e


if ! command -v docker &> /dev/null; then
    echo "=== Installing Docker ==="
    apt-get update
    apt-get install -y docker.io
    systemctl enable docker
    systemctl start docker
fi


if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
    echo "=== Installing Docker Compose ==="
    COMPOSE_VERSION="v2.29.1"
    mkdir -p /usr/local/lib/docker/cli-plugins
    curl -SL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-$(uname -s)-$(uname -m)" \
        -o /usr/local/lib/docker/cli-plugins/docker-compose
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi

echo "=== Setting up application ==="
APP_DIR="/opt/apguru-analytics"
mkdir -p "$APP_DIR"

# If running from cloned repo, copy files
if [ -f "docker-compose.yml" ]; then
    cp -r . "$APP_DIR/"
fi

cd "$APP_DIR"

# Remind to create .env
if [ ! -f ".env" ]; then
    echo ""
    echo "WARNING: No .env file found!"
    echo "Copy .env.example to .env and fill in your values:"
    echo "  cp .env.example .env"
    echo "  nano .env"
    echo ""
    exit 1
fi

echo "=== Building and starting services ==="
docker compose up -d --build

echo ""
echo "=== Deployment complete ==="
echo "Health check: curl http://localhost/api/v1/health"
echo "View logs:    docker compose logs -f"
