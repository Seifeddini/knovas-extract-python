# OSS-Fuzz build container for knovas-extract.
# Copy to projects/knovas-extract/Dockerfile in a google/oss-fuzz fork.

FROM gcr.io/oss-fuzz-base/base-builder-python

# libmagic for python-magic (HTML/EML MIME detection).
RUN apt-get update && \
    apt-get install -y --no-install-recommends libmagic1 && \
    rm -rf /var/lib/apt/lists/*

# Clone the latest release branch — OSS-Fuzz pins this to a commit at scan time.
RUN git clone --depth=1 https://github.com/Seifeddini/knovas-extract-python.git knovas-extract-python
WORKDIR $SRC/knovas-extract-python

COPY build.sh $SRC/
