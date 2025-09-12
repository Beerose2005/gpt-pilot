# Use Ubuntu 22.04 as the base image with multi-arch support
FROM ubuntu:22.04

# Use buildx args for multi-arch support
ARG TARGETPLATFORM
ARG BUILDPLATFORM

# Set defaults for TARGETPLATFORM to ensure it's available in scripts
ENV TARGETPLATFORM=${TARGETPLATFORM:-linux/amd64}

# Copy VSIX file first
COPY pythagora-vs-code.vsix /var/init_data/pythagora-vs-code.vsix

# Install all dependencies
COPY cloud/setup-dependencies.sh /tmp/setup-dependencies.sh
RUN chmod +x /tmp/setup-dependencies.sh && \
    /tmp/setup-dependencies.sh && \
    rm /tmp/setup-dependencies.sh

ENV PYTH_INSTALL_DIR=/pythagora

# Set up work directory
WORKDIR ${PYTH_INSTALL_DIR}/pythagora-core

# Add Python requirements
ADD requirements.txt .

# Create and activate a virtual environment, then install dependencies
RUN python3 -m venv venv && \
    . venv/bin/activate && \
    pip install -r requirements.txt

# Copy application files
ADD main.py .
ADD core core
ADD pyproject.toml .
ADD cloud/config-docker.json config.json

# Set the virtual environment to be automatically activated
ENV VIRTUAL_ENV=${PYTH_INSTALL_DIR}/pythagora-core/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

ENV PYTHAGORA_DATA_DIR=${PYTH_INSTALL_DIR}/pythagora-core/data/
RUN mkdir -p data

# Expose MongoDB and application ports
EXPOSE 27017 8000 8080 5173 3000

# Create a group and user
RUN groupadd -g 1000 devusergroup && \
    useradd -m -u 1000 -g devusergroup -s /bin/bash devuser && \
    echo "devuser:devuser" | chpasswd && \
    adduser devuser sudo && \
    echo "devuser ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Set up entrypoint and VS Code extension
ADD cloud/entrypoint.sh /entrypoint.sh
ADD cloud/on-event-extension-install.sh /var/init_data/on-event-extension-install.sh
ADD cloud/favicon.svg /favicon.svg
ADD cloud/favicon.ico /favicon.ico

# Create necessary directories with proper permissions for code-server
RUN mkdir -p /usr/local/share/code-server/data/User/globalStorage && \
    mkdir -p /usr/local/share/code-server/data/User/History && \
    mkdir -p /usr/local/share/code-server/data/Machine && \
    mkdir -p /usr/local/share/code-server/data/logs

# Add code server settings.json
ADD cloud/settings.json /usr/local/share/code-server/data/Machine/settings.json

RUN chown -R devuser:devusergroup /usr/local/share/code-server && \
    chmod -R 755 /usr/local/share/code-server && \
    # Copy icons
    cp -f /favicon.ico /usr/local/lib/code-server/src/browser/media/favicon.ico && \
    cp -f /favicon.svg /usr/local/lib/code-server/src/browser/media/favicon-dark-support.svg && \
    cp -f /favicon.svg /usr/local/lib/code-server/src/browser/media/favicon.svg

# Configure PostHog analytics integration
RUN sed -i "s|'sha256-/r7rqQ+yrxt57sxLuQ6AMYcy/lUpvAIzHjIJt/OeLWU=' ;|'sha256-/r7rqQ+yrxt57sxLuQ6AMYcy/lUpvAIzHjIJt/OeLWU=' https://us-assets.i.posthog.com ;|g" /usr/local/lib/code-server/lib/vscode/out/server-main.js

COPY cloud/posthog.html /tmp/posthog.html
RUN sed -i '/<head>/r /tmp/posthog.html' /usr/local/lib/code-server/lib/vscode/out/vs/code/browser/workbench/workbench.html && \
    rm /tmp/posthog.html

RUN chmod +x /entrypoint.sh && \
    chmod +x /var/init_data/on-event-extension-install.sh && \
    chown -R devuser:devusergroup /pythagora && \
    chown -R devuser: /var/init_data/

# Create workspace directory
RUN mkdir -p ${PYTH_INSTALL_DIR}/pythagora-core/workspace && \
    chown -R devuser:devusergroup ${PYTH_INSTALL_DIR}/pythagora-core/workspace

# Set up git config
RUN su -c "git config --global user.email 'devuser@pythagora.ai'" devuser && \
    su -c "git config --global user.name 'pythagora'" devuser

# Remove the USER directive to keep root as the running user
ENTRYPOINT ["/entrypoint.sh"]