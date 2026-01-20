# Use Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies required for pjsua2
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    wget \
    pkg-config \
    libssl-dev \
    swig \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Install PJSIP with Python bindings
RUN wget https://github.com/pjsip/pjproject/archive/refs/tags/2.16.tar.gz && \
    tar -xzf 2.16.tar.gz && \
    cd pjproject-2.16 && \
    ./configure --enable-shared --disable-video --disable-libyuv CFLAGS="-O2 -DNDEBUG" && \
    make dep && \
    make && \
    make install && \
    ldconfig && \
    cd pjsip-apps/src/swig/python && \
    make && \
    make install && \
    cd /app && \
    rm -rf /app/pjproject-2.16 /app/2.16.tar.gz

# Copy project files
COPY requirements.txt pyproject.toml README.md ./
COPY src ./src

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install the package
RUN pip install --no-cache-dir -e .

# Expose SIP port (UDP)
EXPOSE 5060/udp
EXPOSE 5060/tcp

# Expose RTP media port range (UDP)
EXPOSE 10000-11000/udp

# Set environment variables (these should be overridden at runtime)
ENV AZURE_VOICELIVE_API_KEY=""
ENV AZURE_VOICELIVE_ENDPOINT=""
ENV SIP_LOCAL_ADDRESS="0.0.0.0"
ENV SIP_PORT="5060"

# Run the gateway
CMD ["python", "-m", "voicelive_sip_gateway.gateway.main"]
