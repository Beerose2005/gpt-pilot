#!/bin/bash
set -e

# Set environment variables
export DEBIAN_FRONTEND=noninteractive
export TZ=Etc/UTC

# IMPORTANT: Create a dummy update-ca-certificates script that does nothing
# This completely bypasses the problematic ca-certificates update process
echo "Creating dummy update-ca-certificates script"
if [ -f /usr/sbin/update-ca-certificates ]; then
    mv /usr/sbin/update-ca-certificates /usr/sbin/update-ca-certificates.orig
fi
cat > /usr/sbin/update-ca-certificates << 'EOF'
#!/bin/sh
echo "Dummy update-ca-certificates called, doing nothing"
exit 0
EOF
chmod +x /usr/sbin/update-ca-certificates

# Update package list and install prerequisites
echo "Installing basic packages"
apt-get update && apt-get install -y --no-install-recommends \
    software-properties-common \
    build-essential \
    curl \
    git \
    gnupg \
    tzdata \
    inotify-tools \
    vim \
    nano \
    lsof \
    procps \
    ca-certificates
echo "Basic packages installed successfully"
rm -rf /var/lib/apt/lists/*

# Install Python 3.12
echo "Adding Python 3.12 PPA"
add-apt-repository ppa:deadsnakes/ppa -y && apt-get update
echo "Installing Python 3.12"
apt-get install -y --no-install-recommends \
    python3.12 \
    python3.12-venv \
    python3.12-dev \
    python3-pip
echo "Python 3.12 installed successfully"
rm -rf /var/lib/apt/lists/*

# Set Python 3.12 as default
echo "Setting Python 3.12 as default"
update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1
update-alternatives --install /usr/bin/python python /usr/bin/python3 1
python --version

# Install Node.js
echo "Installing Node.js"
curl -fsSL https://deb.nodesource.com/setup_lts.x | bash -
apt-get install -y nodejs
node --version && npm --version

# Install MongoDB based on platform architecture
echo "Installing MongoDB"
case "$TARGETPLATFORM" in
    "linux/amd64")
        MONGO_ARCH="amd64"
        ;;
    "linux/arm64"|"linux/arm64/v8")
        MONGO_ARCH="arm64"
        ;;
    *)
        echo "Using default architecture amd64 for MongoDB"
        MONGO_ARCH="amd64"
        ;;
esac

curl -fsSL https://www.mongodb.org/static/pgp/server-6.0.asc | gpg --dearmor -o /usr/share/keyrings/mongodb-archive-keyring.gpg
echo "deb [arch=$MONGO_ARCH signed-by=/usr/share/keyrings/mongodb-archive-keyring.gpg] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/6.0 multiverse" | tee /etc/apt/sources.list.d/mongodb-org-6.0.list
apt-get update && apt-get install -y mongodb-org
echo "MongoDB installed successfully"
rm -rf /var/lib/apt/lists/*

# Install code-server
echo "Installing code-server"
VERSION="4.97.2"
case "$TARGETPLATFORM" in
    "linux/amd64")
        PLATFORM="amd64"
        ;;
    "linux/arm64"|"linux/arm64/v8")
        PLATFORM="arm64"
        ;;
    *)
        echo "Using default platform amd64 for code-server"
        PLATFORM="amd64"
        ;;
esac

DOWNLOAD_URL="https://github.com/coder/code-server/releases/download/v${VERSION}/code-server-${VERSION}-linux-${PLATFORM}.tar.gz"
echo "Downloading code-server from $DOWNLOAD_URL"
curl -L "$DOWNLOAD_URL" -o /tmp/code-server.tar.gz

# Create directories and install
mkdir -p /usr/local/lib/code-server
mkdir -p /usr/local/bin
mkdir -p /usr/local/share/code-server/extensions
mkdir -p /usr/local/share/code-server/data
mkdir -p /etc/code-server

# Install code-server
tar -xzf /tmp/code-server.tar.gz -C /usr/local/lib/code-server --strip-components=1
ln -s /usr/local/lib/code-server/bin/code-server /usr/local/bin/code-server
rm /tmp/code-server.tar.gz
echo "code-server installed successfully"

# Create default config
cat > /etc/code-server/config.yaml << EOF
bind-addr: 0.0.0.0:8080
auth: none
extensions-dir: /usr/local/share/code-server/extensions
user-data-dir: /usr/local/share/code-server/data
EOF

# Pre-install extension
echo "Installing VS Code extension..."
code-server --config /etc/code-server/config.yaml --install-extension /var/init_data/pythagora-vs-code.vsix || {
    echo "Extension installation failed but continuing build process..."
}

# Restore original update-ca-certificates if it exists
if [ -f /usr/sbin/update-ca-certificates.orig ]; then
    mv /usr/sbin/update-ca-certificates.orig /usr/sbin/update-ca-certificates
    echo "Restored original update-ca-certificates"
fi

echo "Setup completed successfully"